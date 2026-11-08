//! The memory writer and the authority protocol (D23).
//!
//! For every write batch: (1) commands arrive prepared; (2) `BEGIN IMMEDIATE`
//! on the memory file; (3) an arbiter turn, then `BEGIN IMMEDIATE` on the hub
//! file; (4) under both locks each command's credential (and, for decisions,
//! the policy) is re-read, the prepared rows are applied and the memory
//! transaction commits at `FULL`; (5) the hub transaction ends with `ROLLBACK`
//! and the turn is returned. The lock order is memory, then hub, and
//! [`HubLock`] is the only handle on the hub connection: it can begin, read
//! and roll back, and has no way to write.
use super::{
    Reply,
    auth::{self, Credential},
    index,
    ops::{self, Authority, Prepared},
};
use crate::writer::{Arbiter, Lane, WriteTx, worker};
use rusqlite::{Connection, ErrorCode};
use std::{
    collections::VecDeque,
    panic::{AssertUnwindSafe, catch_unwind},
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::sync::{mpsc, oneshot};

/// Step boundaries of the protocol, for interleaving tests.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Step {
    AutoPrepared,
    MemoryLocked,
    HubLocked,
    Applied,
    Committed,
    Released,
}
pub type Hook = Arc<dyn Fn(Step) + Send + Sync>;

#[derive(Clone)]
pub struct Config {
    pub memory: PathBuf,
    pub hub: PathBuf,
    pub admin_digest: String,
    pub capacity: usize,
    pub scanner: Arc<dyn super::scan::Scanner>,
    pub clock: super::ids::Clock,
    /// Wait for the memory lock: only a truncating checkpoint can hold it.
    pub memory_wait: Duration,
    /// Wait for the hub lock once the turn is granted (the Python upstream).
    pub hub_wait: Duration,
    /// Wait for an arbiter turn, which includes the residence bucket refill.
    pub turn_wait: Duration,
    pub max_commands: usize,
    pub max_new_bytes: usize,
    pub apply_budget: Duration,
    pub hook: Option<Hook>,
    /// The lexical index this writer publishes after each committed batch.
    pub index: Option<Arc<index::Index>>,
    pub dense: Option<Arc<super::dense::Index>>,
    pub proposal_limits: Arc<std::sync::RwLock<super::hardening::Limits>>,
    pub eval_weights: Arc<std::sync::RwLock<serde_json::Value>>,
    /// Read counts the readers accumulate and this writer flushes.
    pub read_counts: Arc<super::read_counts::ReadCounts>,
}
impl Config {
    pub fn new(memory: PathBuf, hub: PathBuf, admin_digest: String) -> Self {
        Self {
            memory,
            hub,
            admin_digest,
            capacity: 1024,
            scanner: Arc::new(super::scan::Unavailable),
            clock: super::ids::Clock::default(),
            memory_wait: Duration::from_millis(250),
            hub_wait: Duration::from_secs(2),
            turn_wait: Duration::from_secs(5),
            max_commands: 32,
            max_new_bytes: 1024 * 1024,
            apply_budget: Duration::from_millis(10),
            hook: None,
            index: None,
            dense: None,
            proposal_limits: Arc::new(std::sync::RwLock::new(super::hardening::Limits::default())),
            eval_weights: Arc::new(std::sync::RwLock::new(
                super::ranking::Weights::default().json(),
            )),
            read_counts: Arc::default(),
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Failure {
    pub status: u16,
    pub reason: &'static str,
}
pub type Answer = Result<Reply, Failure>;
/// Residence and wait of the hub lock for memory batches, in seconds.
#[derive(Clone, Debug, Default)]
pub struct Metrics {
    pub batches: u64,
    pub commands: u64,
    pub residence_last: f64,
    pub residence_max: f64,
    pub residence_sum: f64,
    pub wait_max: f64,
    pub authority_timeouts: u64,
    pub commit_failures: u64,
    pub sweeps: u64,
    /// The most commands ever held in the writer's own queue, beside the channel.
    pub pending_max: u64,
}
pub enum Job {
    Write {
        command: Box<Prepared>,
        deadline: Instant,
        reply: oneshot::Sender<Answer>,
    },
    /// Bounded retention, run on this writer and never under the hub lock.
    Sweep {
        now_ms: i64,
    },
    /// Write accumulated read counts. Low priority: see [`super::read_counts::due`].
    ReadFlush,
    /// A background index build finished; this writer replays its tail.
    Rebuilt(Box<index::Rebuilt>),
    DenseRebuilt(Box<super::dense::Rebuilt>),
}

/// The only handle on the memory writer's hub connection.
pub(crate) struct HubLock {
    db: Connection,
}
impl HubLock {
    fn open(path: &std::path::Path) -> rusqlite::Result<Self> {
        Ok(Self {
            db: worker::open(path)?,
        })
    }
    fn begin(&self, deadline: Instant) -> rusqlite::Result<()> {
        worker::begin(&self.db, deadline)
    }
    fn reread(
        &self,
        credential: &Credential,
        admin: &str,
        policy: bool,
        automatic: Option<&(Authority, i64)>,
    ) -> Result<Authority, Answer> {
        let deadline = super::hardening::deadline(50);
        if automatic.is_some() {
            self.db
                .progress_handler(1000, Some(move || Instant::now() >= deadline))
                .map_err(|_| Err(storage("policy-reread")))?;
        }
        let result = (|| {
            let tx = worker::Tx(&self.db);
            match auth::still_valid(&tx, credential, admin) {
                Ok(Ok(())) => {}
                Ok(Err(refusal)) => return Err(Ok(refusal)),
                Err(_) => return Err(Err(storage("credential-reread"))),
            }
            // A policy that cannot be read under the lock refuses the command. No
            // policy row at all means no policy is configured.
            if let Some((prepared, version)) = automatic {
                let current = self
                    .db
                    .pragma_query_value(None, "data_version", |r| r.get::<_, i64>(0))
                    .map_err(|_| Err(storage("policy-reread")))?;
                if current == *version {
                    return Ok(prepared.clone());
                }
                // A changed authority file invalidates the prepared proof. No
                // credential enumeration or evidence resolution under this lock.
                return Authority::read(&tx).map_err(|_| Err(storage("policy-reread")));
            }
            if policy {
                Authority::with_credentials(&tx, admin).map_err(|_| Err(storage("policy-reread")))
            } else {
                Ok(Authority::default())
            }
        })();
        let _ = self.db.progress_handler(0, None::<fn() -> bool>);
        result
    }
    /// Ends the read-only hub transaction. Nothing was written to roll back.
    fn release(&self) {
        let _ = self.db.execute_batch("ROLLBACK");
    }
}

/// Publish the flagged principal ids `db` sees, plus `retained`, beside the
/// memory file, labeled with the newest of `db`'s generation and `announced`.
fn publish_set(
    db: &Connection,
    memory: &std::path::Path,
    retained: &std::collections::BTreeSet<String>,
    announced: i64,
) -> Result<(), String> {
    let tx = worker::Tx(db);
    let mut list = super::verifier::principals(&tx).map_err(|e| e.to_string())?;
    let generation = super::verifier::db_generation(&tx)
        .map_err(|e| e.to_string())?
        .max(announced);
    list.extend(retained.iter().cloned());
    list.sort();
    list.dedup();
    super::verifier::publish(memory, generation, &list).map_err(|e| e.to_string())
}

/// Publish the committed set at the hub's current generation, read through
/// `hub`. Startup and every batch that held a flag change call this. A flag
/// change that never committed announced a generation its list can now carry,
/// because the committed set is what is true.
fn publish_verifiers(
    db: &Connection,
    hub: &Connection,
    memory: &std::path::Path,
) -> Result<(), String> {
    let announced = super::verifier::hub_generation(&worker::Tx(hub)).map_err(|e| e.to_string())?;
    publish_set(db, memory, &Default::default(), announced)
}

/// A flag change publishes inside its own savepoint, before the batch commits.
/// The list holds what the transaction flags plus every principal this batch
/// cleared, so a later commit failure can only over-report. If publication
/// fails, the caller rolls the change back and the request fails.
fn publish_pending(
    db: &Connection,
    memory: &std::path::Path,
    action: &ops::Action,
    retained: &mut std::collections::BTreeSet<String>,
) -> Result<(), Reply> {
    let ops::Action::Verifier {
        principal,
        verifier,
        ..
    } = action
    else {
        return Ok(());
    };
    if !verifier {
        retained.insert(principal.clone());
    }
    // The change stored its announced generation in this transaction.
    publish_set(db, memory, retained, 0).map_err(|e| {
        eprintln!("WARN memory verifier list not published, flag change refused: {e}");
        ops::error(
            503,
            "memory_verifier_publish_failed",
            "the verifier list could not be published for the policy writer, so the flag was not changed",
        )
    })
}

fn storage(reason: &'static str) -> Failure {
    Failure {
        status: 503,
        reason,
    }
}
fn hook(config: &Config, step: Step) {
    if let Some(h) = &config.hook {
        h(step);
    }
}

pub struct Writer {
    sender: Option<mpsc::Sender<Job>>,
    thread: Option<std::thread::JoinHandle<()>>,
    pub metrics: Arc<Mutex<Metrics>>,
}
impl Drop for Writer {
    /// Queued commands are answered, then the thread closes its connections.
    fn drop(&mut self) {
        self.sender.take();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}
impl Writer {
    pub fn start(
        config: Config,
        memory: Connection,
        arbiter: Arc<Arbiter>,
    ) -> rusqlite::Result<Self> {
        let hub = HubLock::open(&config.hub)?;
        // Replace a list a crash may have left stale (phase-memory-verify D4)
        // before the store accepts its first request. If that fails, memory
        // does not start: the stale list's generation holds policy writes.
        publish_verifiers(&memory, &hub.db, &config.memory).map_err(|e| {
            eprintln!("memory verifier list not published at startup: {e}");
            rusqlite::Error::ToSqlConversionFailure(e.into())
        })?;
        let (sender, receiver) = mpsc::channel(config.capacity);
        if let Some(index) = &config.index {
            index.attach(sender.downgrade());
        }
        if let Some(dense) = &config.dense {
            dense.attach(sender.downgrade());
        }
        let metrics = Arc::new(Mutex::new(Metrics::default()));
        let m = metrics.clone();
        let thread = std::thread::Builder::new()
            .name("memory-writer".into())
            .spawn(move || run(memory, hub, receiver, config, arbiter, m))
            .map_err(|e| rusqlite::Error::ToSqlConversionFailure(Box::new(e)))?;
        Ok(Self {
            sender: Some(sender),
            thread: Some(thread),
            metrics,
        })
    }
    pub async fn submit(&self, command: Prepared, deadline: Instant) -> Answer {
        let (reply, receive) = oneshot::channel();
        let sender = self
            .sender
            .as_ref()
            .ok_or(storage("memory-writer-stopped"))?;
        sender
            .try_send(Job::Write {
                command: Box::new(command),
                deadline,
                reply,
            })
            .map_err(|e| match e {
                mpsc::error::TrySendError::Full(_) => storage("memory-queue-full"),
                mpsc::error::TrySendError::Closed(_) => storage("memory-writer-stopped"),
            })?;
        receive
            .await
            .unwrap_or(Err(storage("memory-writer-stopped")))
    }
    pub fn sweep(&self, now_ms: i64) -> bool {
        self.sender
            .as_ref()
            .is_some_and(|s| s.try_send(Job::Sweep { now_ms }).is_ok())
    }
    /// Jobs waiting in the channel.
    #[cfg(test)]
    pub(super) fn queued(&self) -> usize {
        self.sender
            .as_ref()
            .map_or(0, |s| s.max_capacity() - s.capacity())
    }
    /// Asks for a read-count flush. A full queue skips it; the next tick asks again.
    pub fn flush_reads(&self) -> bool {
        self.sender
            .as_ref()
            .is_some_and(|s| s.try_send(Job::ReadFlush).is_ok())
    }
}

struct Pending {
    command: Box<Prepared>,
    deadline: Instant,
    reply: oneshot::Sender<Answer>,
    auto_authority: Option<(Authority, i64)>,
}
fn answer(p: Pending, result: Answer) {
    let _ = p.reply.send(result);
}

/// Rows one retention transaction deletes at most.
const SWEEP_BATCH: usize = 256;
#[cfg(test)]
pub(super) fn sweep(db: &Connection, now_ms: i64) -> rusqlite::Result<usize> {
    sweep_with(db, now_ms, &super::hardening::Limits::default())
}
fn sweep_with(
    db: &Connection,
    now_ms: i64,
    limits: &super::hardening::Limits,
) -> rusqlite::Result<usize> {
    worker::begin(db, Instant::now() + Duration::from_millis(250))?;
    // At most 256 rows or 20 ms per transaction, never under the hub lock.
    let deadline = Instant::now() + Duration::from_millis(20);
    let _ = db.progress_handler(1000, Some(move || Instant::now() >= deadline));
    let deleted = db.execute(
        "DELETE FROM memory_idempotency WHERE (principal,client_event_id) IN (SELECT principal,client_event_id FROM memory_idempotency WHERE created_ms < ?1 ORDER BY created_ms LIMIT ?2)",
        rusqlite::params![now_ms - super::ids::RETENTION_MS, SWEEP_BATCH as i64],
    );
    let deleted = deleted.and_then(|n| {
        let jobs = db.execute("DELETE FROM memory_embed_jobs WHERE (content_hash,embedder_id) IN (SELECT content_hash,embedder_id FROM memory_embed_jobs WHERE state='done' AND finished_ms < ?1 LIMIT ?2)", rusqlite::params![now_ms - 7*24*3600*1000, (SWEEP_BATCH-n) as i64])?;
        // Keep the newest log rows. A matrix further behind reloads in full.
        let log = db.execute("DELETE FROM memory_vector_log WHERE seq IN (SELECT seq FROM memory_vector_log WHERE seq <= (SELECT max(seq) FROM memory_vector_log) - ?1 ORDER BY seq LIMIT ?2)", rusqlite::params![super::dense::LOG_KEEP, (SWEEP_BATCH - n - jobs) as i64])?;
        let compacted=super::hardening::compact(db,now_ms,limits,SWEEP_BATCH-n-jobs-log)?;
        Ok(n + jobs + log + compacted)
    });
    let _ = db.progress_handler(0, None::<fn() -> bool>);
    match deleted {
        Ok(n) => match db.execute_batch("COMMIT") {
            Ok(()) => Ok(n),
            // SQLite can leave the transaction open after a failed COMMIT
            // (SQLITE_FULL, an I/O error). End it, as the batch path does, or
            // every later BEGIN IMMEDIATE on this connection fails.
            Err(e) => {
                let _ = db.execute_batch("ROLLBACK");
                Err(e)
            }
        },
        Err(e) => {
            let _ = db.execute_batch("ROLLBACK");
            Err(e)
        }
    }
}

/// Apply one command inside its savepoint. A refusal or error rolls the
/// savepoint back, so only successful commands leave rows behind.
fn apply_one(
    db: &Connection,
    p: &Prepared,
    authority: &Authority,
    scanner: &dyn super::scan::Scanner,
    embedder: Option<(&str, bool)>,
    verifiers: (&std::path::Path, &mut std::collections::BTreeSet<String>),
) -> Answer {
    if db.execute_batch("SAVEPOINT command").is_err() {
        return Err(storage("storage-error"));
    }
    let deadline = super::hardening::deadline(authority.proposal_limits.lock_ms);
    // Review and ordinary writes retain their existing admission contracts.
    // The resource guard covers reflect-controlled automatic work and admission.
    if matches!(&p.action,ops::Action::Proposal(c) if matches!(c.name(),"auto_apply"|"propose"))
        && db
            .progress_handler(1000, Some(move || Instant::now() >= deadline))
            .is_err()
    {
        let _ = db.execute_batch("ROLLBACK TO command; RELEASE command");
        return Err(storage("command-failed"));
    }
    let result = catch_unwind(AssertUnwindSafe(|| {
        ops::apply(
            &worker::Tx(db) as &dyn WriteTx,
            p,
            authority,
            scanner,
            embedder,
        )
    }));
    let _ = db.progress_handler(0, None::<fn() -> bool>);
    let interrupted = matches!(&result,Ok(Err(e)) if e.sqlite_error_code()==Some(ErrorCode::OperationInterrupted));
    if db.is_autocommit() {
        return if interrupted {
            Ok(ops::error(
                503,
                "memory_auto_apply_budget_exceeded",
                "writer scan budget exceeded",
            ))
        } else {
            Err(storage("storage-error"))
        };
    }
    let result = match result {
        Ok(Ok(reply)) => Ok(reply),
        _ if interrupted => Ok(ops::error(
            503,
            "memory_auto_apply_budget_exceeded",
            "writer scan budget exceeded",
        )),
        _ => Err(storage("command-failed")),
    };
    // phase-memory-verify D4: a flag change never commits unpublished.
    let result = match result {
        Ok(reply) if reply.status < 400 => Ok(
            match publish_pending(db, verifiers.0, &p.action, verifiers.1) {
                Ok(()) => reply,
                Err(refusal) => refusal,
            },
        ),
        other => other,
    };
    let keep = matches!(&result, Ok(r) if r.status < 400);
    let finish = if keep {
        "RELEASE command"
    } else {
        "ROLLBACK TO command; RELEASE command"
    };
    if db.execute_batch(finish).is_err() {
        return Err(storage("storage-error"));
    }
    result
}

fn reopen(config: &Config) -> rusqlite::Result<Connection> {
    let db = worker::open(&config.memory)?;
    let check: String = db.query_row("PRAGMA quick_check", [], |r| r.get(0))?;
    if check != "ok" {
        return Err(rusqlite::Error::InvalidQuery);
    }
    Ok(db)
}

fn prepare_auto(
    cmd: &mut super::proposals::Command,
    who: &Credential,
    event: &str,
    hub: &HubLock,
    config: &Config,
    rate: &mut super::hardening::Rate,
) -> Option<(Authority, i64)> {
    use super::hardening as h;
    use serde_json::json;
    let Ok(body) = &mut cmd.draft else {
        return None;
    };
    let mut prepared_authority = None;
    let limits = config.proposal_limits.read().unwrap().clone();
    let result = (|| -> rusqlite::Result<Option<Reply>> {
        let db = Connection::open_with_flags(
            &config.memory,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )?;
        db.execute_batch("BEGIN")?;
        let tx = worker::Tx(&db);
        // Committed retries need no diagnostic work and retain replay semantics.
        if !crate::writer::ReadTx::query(
            &tx,
            "SELECT 1 FROM memory_idempotency WHERE principal=?1 AND client_event_id=?2",
            &[ops::t(who.principal()), ops::t(event)],
        )?
        .is_empty()
        {
            return Ok(None);
        }
        if body["state"] != "pending" {
            return Ok(None);
        }
        if !rate.admit(
            who.principal(),
            super::proposals::string(body, "proposal_id"),
            config.clock.now_ms(),
            &limits,
        ) {
            return Ok(Some(ops::error(
                429,
                "memory_auto_apply_rate_limited",
                "automatic apply attempt rate exceeded",
            )));
        }
        let deadline = h::deadline(limits.diagnostic_ms);
        db.progress_handler(1000, Some(move || Instant::now() >= deadline))?;
        hub.db
            .progress_handler(1000, Some(move || Instant::now() >= deadline))?;
        let budget = h::Budget {
            db: &db,
            remaining: std::cell::Cell::new(limits.diagnostic_rows),
            deadline,
        };
        let hub_version = hub
            .db
            .pragma_query_value(None, "data_version", |r| r.get::<_, i64>(0))?;
        let mut a = Authority::read(&worker::Tx(&hub.db))?;
        a.retrieval = Some(super::retrieval_eval::Live {
            indexed_seq: config.index.as_ref().map_or(0, |i| i.current().indexed_seq),
            embedder_id: config.dense.as_ref().and_then(|d| d.active_id()),
            weights: config.eval_weights.read().unwrap().clone(),
        });
        let report = if let Some(reason) = h::cheap(&budget, body, &a)? {
            super::hardening::held(body, reason)
        } else {
            let retrieval = a.retrieval.clone();
            a = Authority::with_credentials(&worker::Tx(&hub.db), &config.admin_digest)?;
            a.retrieval = retrieval;
            super::preflight::evaluate(
                &budget,
                body,
                &a,
                config.scanner.as_ref(),
                config.clock.now_ms(),
            )?
        };
        prepared_authority = Some((a.clone(), hub_version));
        body["_auto_snapshot"] = json!({"report":report,"token":h::token(&budget)?,"authority":h::authority_digest(&a),"proposal_sha256":super::ids::sha256_hex(super::ids::canonical(body).as_bytes()),"now_ms":config.clock.now_ms()});
        Ok(None)
    })();
    let _ = hub.db.progress_handler(0, None::<fn() -> bool>);
    match result {
        Ok(Some(r)) => cmd.draft = Err(r),
        Ok(None) => (),
        Err(_) => {
            cmd.draft = Err(ops::error(
                503,
                "memory_auto_apply_budget_exceeded",
                "automatic diagnostics exceeded their read budget",
            ))
        }
    }
    prepared_authority
}

pub(crate) fn run(
    mut db: Connection,
    hub: HubLock,
    mut receiver: mpsc::Receiver<Job>,
    config: Config,
    arbiter: Arc<Arbiter>,
    metrics: Arc<Mutex<Metrics>>,
) {
    let mut writes: VecDeque<Pending> = VecDeque::new();
    let mut sweeps: VecDeque<i64> = VecDeque::new();
    let mut rebuilt: Vec<Box<index::Rebuilt>> = Vec::new();
    let mut dense_rebuilt: Vec<Box<super::dense::Rebuilt>> = Vec::new();
    let mut directory_synced = false;
    let mut auto_rate = super::hardening::Rate::default();
    let mut flush_requested = false;
    let mut read_cursor = super::read_counts::SweepCursor::default();
    // Index publication runs on this thread after step 5, never under a lock.
    let mut publisher = config.index.as_ref().and_then(|i| i.publisher());
    let accept = |job: Job,
                  writes: &mut VecDeque<Pending>,
                  sweeps: &mut VecDeque<i64>,
                  rebuilt: &mut Vec<Box<index::Rebuilt>>,
                  dense_rebuilt: &mut Vec<Box<super::dense::Rebuilt>>,
                  flush: &mut bool| match job {
        Job::Write {
            command,
            deadline,
            reply,
        } => writes.push_back(Pending {
            command,
            deadline,
            reply,
            auto_authority: None,
        }),
        Job::Sweep { now_ms } => {
            if sweeps.len() < 4 {
                sweeps.push_back(now_ms);
            }
        }
        Job::ReadFlush => *flush = true,
        Job::Rebuilt(r) => rebuilt.push(r),
        Job::DenseRebuilt(r) => dense_rebuilt.push(r),
    };
    loop {
        // Take from the channel only what the next batch can use, so overload
        // stays in the bounded channel and submitters get memory-queue-full.
        while writes.len() < config.max_commands {
            let Ok(job) = receiver.try_recv() else {
                break;
            };
            accept(
                job,
                &mut writes,
                &mut sweeps,
                &mut rebuilt,
                &mut dense_rebuilt,
                &mut flush_requested,
            );
        }
        for r in rebuilt.drain(..) {
            if let Some(p) = publisher.as_mut() {
                p.installed(*r, &db);
            }
        }
        for rebuilt in dense_rebuilt.drain(..) {
            if let Some(dense) = &config.dense {
                dense.installed(*rebuilt, &db);
            }
        }
        {
            let mut m = metrics.lock().unwrap();
            m.pending_max = m.pending_max.max(writes.len() as u64);
        }
        // Read counts flush one bounded transaction at a time and only while
        // nothing is queued, however full the accumulator is. A flush that sees
        // a job arrive rolls back and yields, so a write never waits behind it.
        if super::read_counts::due(flush_requested, !writes.is_empty()) {
            let waiting = || !receiver.is_empty();
            flush_requested = super::read_counts::flush_once(&db, &config.read_counts, &waiting)
                .is_ok_and(|flush| flush.more);
            continue;
        }
        if writes.is_empty() {
            if let Some(now_ms) = sweeps.pop_front() {
                if let Ok(deleted) =
                    sweep_with(&db, now_ms, &config.proposal_limits.read().unwrap())
                {
                    metrics.lock().unwrap().sweeps += 1;
                    let waiting = || !receiver.is_empty();
                    let reads = super::read_counts::sweep(
                        &db,
                        &config.read_counts,
                        now_ms,
                        &mut read_cursor,
                        &waiting,
                    );
                    // A full batch can leave expired rows behind. Run the next
                    // bounded batch while the writer is idle; writes still go first.
                    if deleted == SWEEP_BATCH || reads.is_ok_and(|r| r.more) {
                        sweeps.push_front(now_ms);
                    }
                }
                continue;
            }
            match receiver.blocking_recv() {
                Some(job) => {
                    accept(
                        job,
                        &mut writes,
                        &mut sweeps,
                        &mut rebuilt,
                        &mut dense_rebuilt,
                        &mut flush_requested,
                    );
                    continue;
                }
                None => return,
            }
        }
        // Gather one batch: at most 32 commands and 1 MiB of new bytes. A
        // single larger command runs alone.
        let mut batch: Vec<Pending> = Vec::new();
        let mut bytes = 0;
        while let Some(next) = writes.front() {
            let automatic =
                matches!(&next.command.action,ops::Action::Proposal(c) if c.name()=="auto_apply");
            if automatic && !batch.is_empty() {
                break;
            }
            let size = next.command.action.new_bytes();
            if !batch.is_empty()
                && (batch.len() >= config.max_commands || bytes + size > config.max_new_bytes)
            {
                break;
            }
            let next = writes.pop_front().unwrap();
            if Instant::now() >= next.deadline {
                answer(next, Err(storage("writer-timeout")));
                continue;
            }
            bytes += size;
            batch.push(next);
            if automatic {
                break;
            }
        }
        if batch.is_empty() {
            continue;
        }
        // Corpus diagnostics run in a separate read snapshot before either writer lock.
        for pending in &mut batch {
            if let ops::Action::Proposal(cmd) = &mut pending.command.action {
                if cmd.name() == "auto_apply" {
                    pending.auto_authority = prepare_auto(
                        cmd,
                        &pending.command.credential,
                        &pending.command.client_event_id,
                        &hub,
                        &config,
                        &mut auto_rate,
                    );
                }
            }
        }
        if batch.iter().any(
            |p| matches!(&p.command.action,ops::Action::Proposal(cmd) if cmd.name()=="auto_apply"),
        ) {
            hook(&config, Step::AutoPrepared);
        }
        // Step 2: the memory lock.
        if let Err(e) = worker::begin(&db, Instant::now() + config.memory_wait) {
            let reason = if e.sqlite_error_code() == Some(ErrorCode::DatabaseBusy) {
                "memory-lock-timeout"
            } else {
                "storage-error"
            };
            for p in batch {
                answer(p, Err(storage(reason)));
            }
            continue;
        }
        if !directory_synced {
            if worker::sync_directory(&db).is_err() {
                let _ = db.execute_batch("ROLLBACK");
                for p in batch {
                    answer(p, Err(storage("storage-error")));
                }
                continue;
            }
            directory_synced = true;
        }
        hook(&config, Step::MemoryLocked);
        // Step 3: an arbiter turn, then the hub lock.
        let waited = Instant::now();
        let turn_deadline = batch
            .iter()
            .map(|p| p.deadline)
            .min()
            .unwrap()
            .min(waited + config.turn_wait);
        let Some(mut turn) = arbiter.acquire(Lane::Memory, turn_deadline) else {
            let _ = db.execute_batch("ROLLBACK");
            metrics.lock().unwrap().authority_timeouts += 1;
            for p in batch {
                answer(p, Err(storage("authority-lock-timeout")));
            }
            continue;
        };
        if let Err(e) = hub.begin(Instant::now() + config.hub_wait) {
            turn.lock_failed();
            let _ = db.execute_batch("ROLLBACK");
            let reason = if e.sqlite_error_code() == Some(ErrorCode::DatabaseBusy) {
                metrics.lock().unwrap().authority_timeouts += 1;
                "authority-lock-timeout"
            } else {
                "storage-error"
            };
            for p in batch {
                answer(p, Err(storage(reason)));
            }
            continue;
        }
        turn.lock_acquired();
        let locked = Instant::now();
        hook(&config, Step::HubLocked);
        // Step 4: re-read authority and apply prepared rows under both locks.
        let mut results: Vec<(Pending, Answer)> = Vec::new();
        // Principals this batch cleared stay published until it commits.
        let mut cleared = std::collections::BTreeSet::new();
        let mut broken = false;
        let mut rest = batch.into_iter();
        for mut p in rest.by_ref() {
            if Instant::now() >= p.deadline {
                results.push((p, Err(storage("writer-timeout"))));
            } else {
                let result = match hub.reread(
                    &p.command.credential,
                    &config.admin_digest,
                    p.command.action.needs_policy() && !matches!(&p.command.action,ops::Action::Proposal(c) if c.name()=="auto_apply"),
                    p.auto_authority.as_ref(),
                ) {
                    Ok(mut authority) => {
                        authority.proposal_limits = config.proposal_limits.read().unwrap().clone();
                        authority.retrieval =
                            config
                                .index
                                .as_ref()
                                .map(|index| super::retrieval_eval::Live {
                                    indexed_seq: index.current().indexed_seq,
                                    embedder_id: config.dense.as_ref().and_then(|d| d.active_id()),
                                    weights: config.eval_weights.read().unwrap().clone(),
                                });
                        // Proof freshness and cooldown use transaction time,
                        // including requests that waited after preparation.
                        if matches!(
                            &p.command.action,
                            ops::Action::Proposal(_)
                                | ops::Action::Evidence(_)
                                | ops::Action::RetrievalEval(_)
                        ) {
                            p.command.now_ms = config.clock.now_ms();
                        }
                        let embedder = config
                            .dense
                            .as_ref()
                            .and_then(|d| d.active_id().map(|id| (id, d.private_ok())));
                        apply_one(
                            &db,
                            &p.command,
                            &authority,
                            config.scanner.as_ref(),
                            embedder
                                .as_ref()
                                .map(|(id, private)| (id.as_str(), *private)),
                            (&config.memory, &mut cleared),
                        )
                    }
                    Err(refusal) => refusal,
                };
                results.push((p, result));
                if db.is_autocommit() {
                    broken = true;
                    break;
                }
            }
            if locked.elapsed() >= config.apply_budget {
                break;
            }
        }
        // Commands left after the apply budget run in the next batch, first.
        for p in rest.rev() {
            writes.push_front(p);
        }
        hook(&config, Step::Applied);
        let committed = !broken && db.execute_batch("COMMIT").is_ok();
        if committed {
            hook(&config, Step::Committed);
        }
        // Any flag change in the batch, applied, refused or replayed, already
        // announced a generation. Republish the committed set at that
        // generation while the hub lock still holds back every hub write. A
        // failure leaves a list whose generation holds policy writes until the
        // next flag change or start replaces it.
        if committed
            && results
                .iter()
                .any(|(p, _)| matches!(p.command.action, ops::Action::Verifier { .. }))
        {
            if let Err(e) = publish_verifiers(&db, &hub.db, &config.memory) {
                eprintln!("WARN memory verifier list not republished: {e}");
            }
        }
        // Step 5: end the hub transaction and return the turn at once, even
        // when the memory commit failed.
        hub.release();
        drop(turn);
        let residence = locked.elapsed().as_secs_f64();
        hook(&config, Step::Released);
        {
            let mut m = metrics.lock().unwrap();
            m.batches += 1;
            m.commands += results.len() as u64;
            m.residence_last = residence;
            m.residence_max = m.residence_max.max(residence);
            m.residence_sum += residence;
            m.wait_max = m.wait_max.max(locked.duration_since(waited).as_secs_f64());
            if !committed {
                m.commit_failures += 1;
            }
        }
        if committed {
            if let Some(dense) = &config.dense {
                dense.after_commit(&db);
            }
            for (p, result) in results {
                answer(p, result);
            }
            if let Some(p) = publisher.as_mut() {
                p.after_commit(&db);
            }
            continue;
        }
        let _ = db.execute_batch("ROLLBACK");
        for (p, result) in results {
            if matches!(&result,Ok(r) if r.body["code"]=="memory_auto_apply_budget_exceeded") {
                answer(p, result);
                continue;
            }
            answer(
                p,
                Err(storage(if broken {
                    "storage-error"
                } else {
                    "commit-failed"
                })),
            );
        }
        drop(db);
        match reopen(&config) {
            Ok(reopened) => {
                db = reopened;
                directory_synced = false;
            }
            Err(_) => {
                receiver.close();
                for p in writes.drain(..) {
                    answer(p, Err(storage("storage-error")));
                }
                while let Some(job) = receiver.blocking_recv() {
                    if let Job::Write { reply, .. } = job {
                        let _ = reply.send(Err(storage("storage-error")));
                    }
                }
                return;
            }
        }
    }
}

#[cfg(test)]
pub(crate) fn hub_lock(path: &std::path::Path) -> rusqlite::Result<HubLock> {
    HubLock::open(path)
}
