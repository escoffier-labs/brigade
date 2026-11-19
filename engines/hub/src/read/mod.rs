//! Dedicated blocking workers with bounded queues. No SQLite work runs on tokio.
mod cpu;
use crate::schema::{self, SchemaGate};
use crate::telemetry::ReaderStats;
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
    hub_threads_for(
        std::thread::available_parallelism()
            .map(usize::from)
            .unwrap_or(1),
    )
}
/// One reader is never enough: any slow handler read holds every other handler
/// read in its queue, so the floor is two even on a single-CPU machine.
fn hub_threads_for(available: usize) -> usize {
    available.clamp(2, 8)
}
pub const MEMORY_THREADS: usize = 2;
pub const HUB_QUEUE: usize = 1024;
pub const MEMORY_QUEUE: usize = 256;
/// Admission looks up a credential on every request. Handler reads cannot
/// starve it while it has its own small pool.
pub const LOOKUP_THREADS: usize = 2;
pub const LOOKUP_QUEUE: usize = 64;

/// The reader pools a hub process opens on its database: handler reads, and the
/// credential lookups admission runs on every request before a route reads.
pub struct HubPools {
    pub readers: ReaderPool,
    pub lookups: ReaderPool,
}
impl HubPools {
    pub fn open(path: &Path) -> Result<Self, String> {
        Ok(Self {
            readers: ReaderPool::open(path, hub_threads(), HUB_QUEUE)?,
            lookups: ReaderPool::open(path, LOOKUP_THREADS, LOOKUP_QUEUE)?,
        })
    }
    /// The same split without the schema check, for tests with a bare file.
    #[cfg(test)]
    fn open_unchecked(path: &Path) -> Result<Self, String> {
        Ok(Self {
            readers: ReaderPool::open_unchecked(path, hub_threads(), HUB_QUEUE, READ_LIMIT)?,
            lookups: ReaderPool::open_unchecked(path, LOOKUP_THREADS, LOOKUP_QUEUE, READ_LIMIT)?,
        })
    }
}

pub struct ReaderPool {
    sender: Option<mpsc::Sender<Job>>,
    workers: Vec<JoinHandle<()>>,
    limit: Duration,
    checked: bool,
    stats: Arc<ReaderStats>,
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
            stats: Arc::default(),
        })
    }
    /// For `Telemetry::attach_readers`.
    pub fn queue_probe(&self) -> crate::telemetry::QueueProbe {
        match &self.sender {
            Some(sender) => crate::telemetry::queue_probe(sender),
            None => Box::new(|| 0),
        }
    }
    /// For `Telemetry::attach_reader_stats`.
    pub fn stats(&self) -> Arc<ReaderStats> {
        self.stats.clone()
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
        let stats = self.stats.clone();
        let job = Box::new(move |c: &mut Connection, cookie: &mut Option<(i64, i64)>| {
            // The deadline counts from when the caller queued the job, not from
            // when a reader picked it up: a caller that waits at most `limit`
            // is never followed by up to `limit` more seconds of work no one
            // will read.
            let deadline = queued + limit;
            if Instant::now() >= deadline {
                // The caller has already given up on this job. Report the queue
                // as the refusal it is and leave the reader for the next job.
                stats.aged();
                let _ = reply.send(Err("reader-queue-full".into()));
                return;
            }
            if reply.is_closed() || started.send(()).is_err() {
                // The caller stopped waiting: its deadline fired or the
                // connection ended. Skip the query instead of delaying the
                // jobs behind it.
                stats.abandoned();
                return;
            }
            stats.ran(queued.elapsed());
            let began = Instant::now();
            if let Err(e) = c.progress_handler(1000, Some(move || Instant::now() >= deadline)) {
                stats.finished(began.elapsed());
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
            stats.finished(began.elapsed());
            let _ = c.progress_handler(0, None::<fn() -> bool>);
            // SQLite may automatically reprepare SQLITE_SCHEMA. The cookie check
            // inside each snapshot also detects changes hidden by that retry.
            if let Err(ref error) = result {
                let _ = SchemaGate::default().on_error(c, error);
            }
            let result = if Instant::now() >= deadline {
                stats.expired();
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
        tokio::time::timeout(limit, self.sender.as_ref().unwrap().send(job))
            .await
            .map_err(|_| "reader-queue-full")?
            .map_err(|_| "reader-stopped")?;
        let remaining = limit.saturating_sub(queued.elapsed());
        tokio::time::timeout(remaining, starting)
            .await
            .map_err(|_| "reader-queue-full")?
            .map_err(|_| "reader-stopped")?;
        // A job that started just before the deadline still owes an answer by
        // then. The runner refuses at the same instant, so either side reports
        // the same reason.
        let remaining = limit.saturating_sub(queued.elapsed());
        tokio::time::timeout(remaining, receive)
            .await
            .map_err(|_| "reader-timeout")?
            .map_err(|_| "reader-stopped")?
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
        mpsc as std_mpsc,
    };

    fn temp_db(name: &str) -> std::path::PathBuf {
        let path =
            std::env::temp_dir().join(format!("hub-read-pool-{}-{name}.db", std::process::id()));
        rusqlite::Connection::open(&path)
            .unwrap()
            .execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS t(x);")
            .unwrap();
        path
    }
    fn wait_for(what: &str, mut done: impl FnMut() -> bool) {
        let deadline = Instant::now() + Duration::from_secs(5);
        while !done() {
            assert!(Instant::now() < deadline, "{what}");
            std::thread::sleep(Duration::from_millis(5));
        }
    }
    /// Holds the pool's only reader until the returned sender fires.
    fn hold_reader(
        pool: &Arc<ReaderPool>,
    ) -> (
        tokio::task::JoinHandle<()>,
        std_mpsc::Sender<()>,
        std_mpsc::Receiver<()>,
    ) {
        let (entered_tx, entered) = std_mpsc::channel::<()>();
        let (gate, released) = std_mpsc::channel::<()>();
        let pool = pool.clone();
        let hold = tokio::spawn(async move {
            // The hold outlives the pool's own deadline, so its refusal is expected.
            let _ = pool
                .submit(move |_| {
                    entered_tx.send(()).unwrap();
                    released.recv().ok();
                    Ok(())
                })
                .await;
        });
        (hold, gate, entered)
    }

    #[test]
    fn hub_threads_never_drop_below_two() {
        // A single-CPU hub still gets two readers, and the cap still holds.
        assert_eq!(hub_threads_for(0), 2);
        assert_eq!(hub_threads_for(1), 2);
        assert_eq!(hub_threads_for(2), 2);
        assert_eq!(hub_threads_for(4), 4);
        assert_eq!(hub_threads_for(24), 8);
        assert!((2..=8).contains(&hub_threads()));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn hub_pools_keep_lookups_off_the_handler_readers() {
        let path = temp_db("pools");
        let HubPools { readers, lookups } = HubPools::open_unchecked(&path).unwrap();
        let readers = Arc::new(readers);
        let lookups = Arc::new(lookups);
        let mut held = Vec::new();
        for _ in 0..hub_threads() {
            held.push(hold_reader(&readers));
        }
        // Every handler reader is now held by one slow job.
        for (_, _, entered) in &held {
            entered.recv_timeout(Duration::from_secs(5)).unwrap();
        }
        let started = Instant::now();
        lookups.submit(|_| Ok(())).await.unwrap();
        assert!(
            started.elapsed() < Duration::from_millis(500),
            "a held handler pool must not delay a credential lookup: {:?}",
            started.elapsed()
        );
        for (hold, gate, _) in held {
            gate.send(()).unwrap();
            hold.await.unwrap();
        }
        drop(readers);
        drop(lookups);
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_job_queued_past_its_deadline_never_runs() {
        let path = temp_db("aged");
        let pool =
            Arc::new(ReaderPool::open_unchecked(&path, 1, 8, Duration::from_millis(100)).unwrap());
        let (hold, gate, entered) = hold_reader(&pool);
        entered.recv_timeout(Duration::from_secs(5)).unwrap();
        let ran = Arc::new(AtomicUsize::new(0));
        let marker = ran.clone();
        let pool_copy = pool.clone();
        let late = tokio::spawn(async move {
            pool_copy
                .submit(move |_| {
                    marker.fetch_add(1, Ordering::SeqCst);
                    Ok(())
                })
                .await
        });
        // Longer than the caller's 100ms budget, shorter than the old 2s one.
        tokio::time::sleep(Duration::from_millis(150)).await;
        gate.send(()).unwrap();
        hold.await.unwrap();
        assert_eq!(
            late.await.unwrap().err().as_deref(),
            Some("reader-queue-full")
        );
        wait_for("the aged job is counted", || {
            pool.stats().snapshot().aged == 1
        });
        assert_eq!(
            ran.load(Ordering::SeqCst),
            0,
            "a job whose caller stopped waiting must not run"
        );
        drop(pool);
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_job_whose_caller_left_is_skipped() {
        let path = temp_db("abandoned");
        let pool = Arc::new(ReaderPool::open_unchecked(&path, 1, 8, READ_LIMIT).unwrap());
        let (hold, gate, entered) = hold_reader(&pool);
        entered.recv_timeout(Duration::from_secs(5)).unwrap();
        let ran = Arc::new(AtomicUsize::new(0));
        let marker = ran.clone();
        let probe = pool.queue_probe();
        let pool_copy = pool.clone();
        let abandoned = tokio::spawn(async move {
            pool_copy
                .submit(move |_| {
                    marker.fetch_add(1, Ordering::SeqCst);
                    Ok(())
                })
                .await
        });
        wait_for("the job is queued", || probe() == 1);
        // Dropping the caller drops the reply channel the job would send to.
        abandoned.abort();
        assert!(abandoned.await.unwrap_err().is_cancelled());
        gate.send(()).unwrap();
        hold.await.unwrap();
        wait_for("the abandoned job is counted", || {
            pool.stats().snapshot().abandoned == 1
        });
        assert_eq!(ran.load(Ordering::SeqCst), 0);
        drop(pool);
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn the_run_deadline_counts_from_the_queue_not_the_start() {
        let path = temp_db("deadline");
        let pool =
            Arc::new(ReaderPool::open_unchecked(&path, 1, 8, Duration::from_millis(400)).unwrap());
        let (hold, gate, entered) = hold_reader(&pool);
        entered.recv_timeout(Duration::from_secs(5)).unwrap();
        let pool_copy = pool.clone();
        let slow = tokio::spawn(async move {
            pool_copy
                .submit(|_| {
                    // A start-relative 400ms deadline would still allow this.
                    std::thread::sleep(Duration::from_millis(250));
                    Ok(())
                })
                .await
        });
        tokio::time::sleep(Duration::from_millis(250)).await;
        gate.send(()).unwrap();
        hold.await.unwrap();
        let started = Instant::now();
        assert_eq!(
            slow.await.unwrap().err().as_deref(),
            Some("reader-timeout"),
            "work past the queued deadline must not answer the caller"
        );
        assert!(started.elapsed() < Duration::from_millis(250));
        drop(pool);
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn reader_stats_count_jobs_and_their_time() {
        let path = temp_db("stats");
        let pool = ReaderPool::open_unchecked(&path, 1, 8, READ_LIMIT).unwrap();
        pool.submit(|_| Ok(7)).await.unwrap();
        let snapshot = pool.stats().snapshot();
        assert_eq!(snapshot.jobs, 1);
        assert!(snapshot.run_micros > 0, "a job records its run time");
        assert_eq!(snapshot.aged, 0);
        assert_eq!(snapshot.expired, 0);
        assert_eq!(snapshot.abandoned, 0);
        drop(pool);
        let _ = std::fs::remove_file(&path);
    }
}
