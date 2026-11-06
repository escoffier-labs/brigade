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
use crate::writer::{Arbiter, Lane, ReadTx, WriteTx, worker};
use rusqlite::{Connection, ErrorCode, types::Value};
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
}
impl Config {
    pub fn new(memory: PathBuf, hub: PathBuf, admin_digest: String) -> Self {
        Self {
            memory,
            hub,
            admin_digest,
            capacity: 1024,
            scanner: Arc::new(super::scan::Unavailable),
            memory_wait: Duration::from_millis(250),
            hub_wait: Duration::from_secs(2),
            turn_wait: Duration::from_secs(5),
            max_commands: 32,
            max_new_bytes: 1024 * 1024,
            apply_budget: Duration::from_millis(10),
            hook: None,
            index: None,
            dense: None,
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
    ) -> Result<Authority, Answer> {
        let tx = worker::Tx(&self.db);
        match auth::still_valid(&tx, credential, admin) {
            Ok(Ok(())) => {}
            Ok(Err(refusal)) => return Err(Ok(refusal)),
            Err(_) => return Err(Err(storage("credential-reread"))),
        }
        // A policy that cannot be read under the lock refuses the command. No
        // policy row at all means no policy is configured.
        let policy = if policy {
            let rows = tx
                .query(
                    "SELECT m.current_revision,r.digest FROM fleet_policy_meta m JOIN fleet_policy_revisions r ON r.revision=m.current_revision WHERE m.singleton=1",
                    &[],
                )
                .map_err(|_| Err(storage("policy-reread")))?;
            match rows.first().map(|r| (&r[0], &r[1])) {
                None => None,
                Some((Value::Integer(rev), Value::Text(digest))) => Some((*rev, digest.clone())),
                Some(_) => return Err(Err(storage("policy-reread"))),
            }
        } else {
            None
        };
        Ok(Authority { policy })
    }
    /// Ends the read-only hub transaction. Nothing was written to roll back.
    fn release(&self) {
        let _ = self.db.execute_batch("ROLLBACK");
    }
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
}

struct Pending {
    command: Box<Prepared>,
    deadline: Instant,
    reply: oneshot::Sender<Answer>,
}
fn answer(p: Pending, result: Answer) {
    let _ = p.reply.send(result);
}

/// Rows one retention transaction deletes at most.
const SWEEP_BATCH: usize = 256;
pub(super) fn sweep(db: &Connection, now_ms: i64) -> rusqlite::Result<usize> {
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
        Ok(n + jobs + log)
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
    embedder: Option<&str>,
) -> Answer {
    if db.execute_batch("SAVEPOINT command").is_err() {
        return Err(storage("storage-error"));
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
    if db.is_autocommit() {
        return Err(storage("storage-error"));
    }
    let result = match result {
        Ok(Ok(reply)) => Ok(reply),
        _ => Err(storage("command-failed")),
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
    // Index publication runs on this thread after step 5, never under a lock.
    let mut publisher = config.index.as_ref().and_then(|i| i.publisher());
    let accept = |job: Job,
                  writes: &mut VecDeque<Pending>,
                  sweeps: &mut VecDeque<i64>,
                  rebuilt: &mut Vec<Box<index::Rebuilt>>,
                  dense_rebuilt: &mut Vec<Box<super::dense::Rebuilt>>| match job {
        Job::Write {
            command,
            deadline,
            reply,
        } => writes.push_back(Pending {
            command,
            deadline,
            reply,
        }),
        Job::Sweep { now_ms } => {
            if sweeps.len() < 4 {
                sweeps.push_back(now_ms);
            }
        }
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
        if writes.is_empty() {
            if let Some(now_ms) = sweeps.pop_front() {
                if let Ok(deleted) = sweep(&db, now_ms) {
                    metrics.lock().unwrap().sweeps += 1;
                    // A full batch can leave expired rows behind. Run the next
                    // bounded batch while the writer is idle; writes still go first.
                    if deleted == SWEEP_BATCH {
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
        }
        if batch.is_empty() {
            continue;
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
        let mut broken = false;
        let mut rest = batch.into_iter();
        for p in rest.by_ref() {
            if Instant::now() >= p.deadline {
                results.push((p, Err(storage("writer-timeout"))));
            } else {
                let result = match hub.reread(
                    &p.command.credential,
                    &config.admin_digest,
                    p.command.action.needs_policy(),
                ) {
                    Ok(authority) => {
                        let embedder = config.dense.as_ref().and_then(|d| d.active_id());
                        apply_one(
                            &db,
                            &p.command,
                            &authority,
                            config.scanner.as_ref(),
                            embedder.as_deref(),
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
        for (p, _) in results {
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
