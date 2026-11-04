//! Bounded group-commit API for S5 domain commands. No routes use it yet.
//! Commands are trusted, replayable database work: no external I/O or side effects.
mod arbiter;
mod worker;
pub use arbiter::{Arbiter, Lane, Turn};
use rusqlite::types::Value;
use serde_json::Value as Json;
use std::{
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::sync::{OwnedSemaphorePermit, Semaphore, mpsc, oneshot};

/// Backend boundary used by domain commands. No connection or transaction control is exposed.
pub trait ReadTx {
    fn query(&self, sql: &str, params: &[Value]) -> rusqlite::Result<Vec<Vec<Value>>>;
}
pub trait WriteTx: ReadTx {
    fn execute(&self, sql: &str, params: &[Value]) -> rusqlite::Result<usize>;
}
/// Apply must re-check credentials and do all decision reads inside this transaction.
/// It may run twice after a whole-transaction abort. Publish wakeups only in post_commit.
pub trait Command: Send + 'static {
    fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError>;
    fn post_commit(&self, _external_change: bool) {}
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Failure {
    pub status: u16,
    pub reason: &'static str,
}
impl Failure {
    fn storage(reason: &'static str) -> Self {
        Self {
            status: 503,
            reason,
        }
    }
    fn timeout(durable: bool) -> Self {
        Self {
            status: if durable { 504 } else { 503 },
            reason: "writer-timeout",
        }
    }
}
/// Domain refusals roll back only their command, just like statement failures.
#[derive(Debug)]
pub enum ApplyError {
    Sqlite(rusqlite::Error),
    Rejected(Failure),
    /// A domain answer whose writes roll back, as a Python exception would.
    /// It is delivered as a successful reply, so post_commit still runs.
    Refused(Json),
}
impl From<rusqlite::Error> for ApplyError {
    fn from(error: rusqlite::Error) -> Self {
        Self::Sqlite(error)
    }
}
pub type Answer = Result<Json, Failure>;
#[derive(Clone, Copy)]
pub struct Budget {
    pub events: usize,
    pub bytes: usize,
    pub durable: bool,
    pub deadline: Instant,
}
#[derive(Clone)]
pub struct Config {
    pub path: PathBuf,
    pub capacity: usize,
    pub durable_reserve: usize,
    pub queue_bytes: usize,
    pub begin_wait: Duration,
}
impl Config {
    pub fn new(path: PathBuf) -> Self {
        Self {
            path,
            capacity: 4096,
            durable_reserve: 512,
            queue_bytes: 64 * 1024 * 1024,
            begin_wait: Duration::from_secs(5),
        }
    }
}
struct Envelope {
    command: Box<dyn Command>,
    budget: Budget,
    reply: Option<oneshot::Sender<Answer>>,
    _permits: Vec<OwnedSemaphorePermit>,
    arbiter: Arc<Arbiter>,
}
impl Drop for Envelope {
    fn drop(&mut self) {
        if self.budget.durable {
            self.arbiter.durable(-1);
        }
    }
}
#[derive(Clone)]
pub struct Writer {
    sender: mpsc::Sender<Envelope>,
    slots: Arc<Semaphore>,
    regular: Arc<Semaphore>,
    bytes: Arc<Semaphore>,
    arbiter: Arc<Arbiter>,
    max_bytes: usize,
}
impl Writer {
    /// Opens an existing, Python-initialized file. Never creates tables or migrates it.
    pub fn start(config: Config, arbiter: Arc<Arbiter>) -> rusqlite::Result<Self> {
        assert!(
            config.capacity > config.durable_reserve && config.queue_bytes <= u32::MAX as usize
        );
        let connection = worker::open(&config.path)?;
        crate::schema::check(&connection)
            .map_err(|e| rusqlite::Error::ToSqlConversionFailure(e.into()))?;
        Self::start_connection(config, arbiter, connection)
    }
    fn start_connection(
        config: Config,
        arbiter: Arc<Arbiter>,
        connection: rusqlite::Connection,
    ) -> rusqlite::Result<Self> {
        let (sender, receiver) = mpsc::channel(config.capacity);
        let writer = Self {
            sender,
            slots: Arc::new(Semaphore::new(config.capacity)),
            regular: Arc::new(Semaphore::new(config.capacity - config.durable_reserve)),
            bytes: Arc::new(Semaphore::new(config.queue_bytes)),
            arbiter: arbiter.clone(),
            max_bytes: config.queue_bytes,
        };
        std::thread::Builder::new()
            .name("hub-writer".into())
            .spawn(move || worker::run(connection, receiver, config, arbiter))
            .map_err(|e| rusqlite::Error::ToSqlConversionFailure(Box::new(e)))?;
        Ok(writer)
    }
    /// Non-durable overload is immediate 503. Durable admission waits to its deadline.
    /// Byte accounting includes command-owned payload and expected reply allocations.
    pub async fn submit(
        &self,
        command: impl Command,
        budget: Budget,
    ) -> Result<oneshot::Receiver<Answer>, Failure> {
        if budget.bytes > self.max_bytes || budget.bytes > 8 * 1024 * 1024 || budget.events > 4096 {
            return Err(Failure {
                status: if budget.durable { 504 } else { 503 },
                reason: "writer-budget",
            });
        }
        let mut permits = Vec::new();
        if !budget.durable {
            permits.push(self.permit(&self.regular, 1, budget).await?);
        }
        permits.push(self.permit(&self.slots, 1, budget).await?);
        permits.push(
            self.permit(&self.bytes, budget.bytes as u32, budget)
                .await?,
        );
        if Instant::now() >= budget.deadline {
            return Err(Failure::timeout(budget.durable));
        }
        let (reply, receiver) = oneshot::channel();
        if budget.durable {
            self.arbiter.durable(1);
        }
        let envelope = Envelope {
            command: Box::new(command),
            budget,
            reply: Some(reply),
            _permits: permits,
            arbiter: self.arbiter.clone(),
        };
        self.sender
            .try_send(envelope)
            .map_err(|_| Failure::storage("writer-stopped"))?;
        Ok(receiver)
    }
    async fn permit(
        &self,
        semaphore: &Arc<Semaphore>,
        count: u32,
        budget: Budget,
    ) -> Result<OwnedSemaphorePermit, Failure> {
        if budget.durable {
            tokio::time::timeout_at(
                budget.deadline.into(),
                semaphore.clone().acquire_many_owned(count),
            )
            .await
            .map_err(|_| Failure::timeout(true))?
            .map_err(|_| Failure::storage("writer-stopped"))
        } else {
            semaphore
                .clone()
                .try_acquire_many_owned(count)
                .map_err(|_| Failure::storage("writer-queue-full"))
        }
    }
}

#[cfg(test)]
mod powercut;
#[cfg(test)]
mod tests;

/// Maps maintenance requests to replayable domain commands. Unsupported work is
/// refused for a later tick, including memory work until its owner is wired.
pub struct SweepWriter<F> {
    writer: Writer,
    command: F,
}
impl Writer {
    pub fn sweeps<F>(&self, command: F) -> SweepWriter<F>
    where
        F: Fn(crate::maintenance::Sweep) -> Option<Box<dyn Command>> + Send + Sync + 'static,
    {
        SweepWriter {
            writer: self.clone(),
            command,
        }
    }
}
impl<F> crate::maintenance::WriterCommands for SweepWriter<F>
where
    F: Fn(crate::maintenance::Sweep) -> Option<Box<dyn Command>> + Send + Sync + 'static,
{
    fn schedule(&self, sweep: crate::maintenance::Sweep) -> bool {
        let Some(command) = (self.command)(sweep) else {
            return false;
        };
        let mut permits = Vec::new();
        // A bounded maintenance command must fit one ordinary queue slot and
        // the same byte reservation as other work. It never waits for admission.
        for (semaphore, count) in [
            (&self.writer.regular, 1),
            (&self.writer.slots, 1),
            (&self.writer.bytes, 8 * 1024 * 1024),
        ] {
            let Ok(permit) = semaphore.clone().try_acquire_many_owned(count) else {
                return false;
            };
            permits.push(permit);
        }
        self.writer
            .sender
            .try_send(Envelope {
                command,
                budget: Budget {
                    events: 4096,
                    bytes: 8 * 1024 * 1024,
                    durable: false,
                    deadline: Instant::now() + Duration::from_millis(250),
                },
                reply: None,
                _permits: permits,
                arbiter: self.writer.arbiter.clone(),
            })
            .is_ok()
    }
}
