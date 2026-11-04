//! Dedicated blocking workers with bounded queues. No SQLite work runs on tokio.
mod cpu;
use crate::schema::{self, SchemaGate};
pub use cpu::CpuPool;
use rusqlite::{Connection, OpenFlags};
use std::{
    path::Path,
    sync::{Arc, Mutex},
    thread::JoinHandle,
    time::{Duration, Instant},
};
use tokio::sync::{mpsc, oneshot};

type Job = Box<dyn FnOnce(&mut Connection, &mut Option<(i64, i64)>) + Send>;
pub const READ_LIMIT: Duration = Duration::from_secs(2);
pub fn hub_threads() -> usize {
    std::thread::available_parallelism()
        .map(usize::from)
        .unwrap_or(1)
        .min(8)
}
pub const MEMORY_THREADS: usize = 2;
pub const HUB_QUEUE: usize = 1024;
pub const MEMORY_QUEUE: usize = 256;

pub struct ReaderPool {
    sender: Option<mpsc::Sender<Job>>,
    workers: Vec<JoinHandle<()>>,
    limit: Duration,
    checked: bool,
}
impl ReaderPool {
    pub fn open(path: &Path, threads: usize, capacity: usize) -> Result<Self, String> {
        Self::build(path, threads, capacity, READ_LIMIT, true)
    }
    /// For files with their own schema owner, such as the future memory file.
    pub fn open_unchecked(
        path: &Path,
        threads: usize,
        capacity: usize,
        limit: Duration,
    ) -> Result<Self, String> {
        Self::build(path, threads, capacity, limit, false)
    }
    fn build(
        path: &Path,
        threads: usize,
        capacity: usize,
        limit: Duration,
        checked: bool,
    ) -> Result<Self, String> {
        if threads == 0 || capacity == 0 || limit.is_zero() {
            return Err("invalid reader limits".into());
        }
        // Open all connections before spawning so partial startup never leaks threads.
        let mut connections = Vec::new();
        for _ in 0..threads {
            let c = schema::audit::open(
                path,
                OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
            )
            .map_err(|e| e.to_string())?;
            c.execute_batch("PRAGMA query_only=ON; PRAGMA wal_autocheckpoint=0;")
                .map_err(|e| e.to_string())?;
            c.busy_timeout(Duration::ZERO).map_err(|e| e.to_string())?;
            c.set_prepared_statement_cache_capacity(64);
            if checked {
                schema::check(&c)?;
            }
            connections.push(c);
        }
        let (sender, receiver) = mpsc::channel::<Job>(capacity);
        let receiver = Arc::new(Mutex::new(receiver));
        let workers = connections
            .into_iter()
            .map(|mut c| {
                let receiver = receiver.clone();
                std::thread::spawn(move || {
                    let mut cookie = None;
                    loop {
                        let job = receiver.lock().unwrap().blocking_recv();
                        match job {
                            Some(job) => job(&mut c, &mut cookie),
                            None => break,
                        }
                    }
                })
            })
            .collect();
        Ok(Self {
            sender: Some(sender),
            workers,
            limit,
            checked,
        })
    }
    pub async fn submit<T: Send + 'static>(
        &self,
        query: impl FnOnce(&Connection) -> rusqlite::Result<T> + Send + 'static,
    ) -> Result<T, String> {
        let (reply, receive) = oneshot::channel();
        let (started, starting) = oneshot::channel();
        let limit = self.limit;
        let queued = Instant::now();
        let checked = self.checked;
        let job = Box::new(move |c: &mut Connection, cookie: &mut Option<(i64, i64)>| {
            if queued.elapsed() >= READ_LIMIT {
                let _ = reply.send(Err("reader-queue-full".into()));
                return;
            }
            if started.send(()).is_err() {
                return;
            }
            let deadline = Instant::now() + limit;

            if let Err(e) = c.progress_handler(1000, Some(move || Instant::now() >= deadline)) {
                let _ = reply.send(Err(e.to_string()));
                return;
            }
            let result = (|| {
                let tx = c.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred)?;
                if checked {
                    let next = (
                        tx.pragma_query_value(None, "schema_version", |r| r.get(0))?,
                        tx.pragma_query_value(None, "user_version", |r| r.get(0))?,
                    );
                    if *cookie != Some(next) {
                        schema::check(&tx).map_err(|_| rusqlite::Error::InvalidQuery)?;
                        *cookie = Some(next);
                        tx.flush_prepared_statement_cache();
                    }
                }
                let value = query(&tx)?;
                tx.commit()?;
                Ok(value)
            })();
            let _ = c.progress_handler(0, None::<fn() -> bool>);
            // SQLite may automatically reprepare SQLITE_SCHEMA. The cookie check
            // inside each snapshot also detects changes hidden by that retry.
            if let Err(ref error) = result {
                let _ = SchemaGate::default().on_error(c, error);
            }
            let result = if Instant::now() >= deadline {
                Err("reader-timeout".into())
            } else {
                result.map_err(|e: rusqlite::Error| {
                    if checked && matches!(e, rusqlite::Error::InvalidQuery) {
                        "schema-changed".into()
                    } else {
                        e.to_string()
                    }
                })
            };
            let _ = reply.send(result);
        });
        tokio::time::timeout(READ_LIMIT, self.sender.as_ref().unwrap().send(job))
            .await
            .map_err(|_| "reader-queue-full")?
            .map_err(|_| "reader-stopped")?;
        let remaining = READ_LIMIT.saturating_sub(queued.elapsed());
        tokio::time::timeout(remaining, starting)
            .await
            .map_err(|_| "reader-queue-full")?
            .map_err(|_| "reader-stopped")?;
        receive.await.map_err(|_| "reader-stopped")?
    }
}
impl Drop for ReaderPool {
    fn drop(&mut self) {
        self.sender.take();
        for worker in self.workers.drain(..) {
            let _ = worker.join();
        }
    }
}
