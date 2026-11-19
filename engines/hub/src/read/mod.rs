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
/// When a read job's budget starts counting.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Deadline {
    /// From when the caller queued the job: a caller that waits at most
    /// `limit` is never followed by up to `limit` more seconds of work no one
    /// will read. Handler and lookup pools use this.
    Queued,
    /// From when a reader starts the job, and the caller's final wait is not
    /// cut short. The memory pool keeps this: a search that queued for most of
    /// its limit still gets a full run window, as it had before the hub split
    /// its pools.
    Started,
}
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
    deadline: Deadline,
    checked: bool,
    stats: Arc<ReaderStats>,
}
impl ReaderPool {
    pub fn open(path: &Path, threads: usize, capacity: usize) -> Result<Self, String> {
        Self::build(path, threads, capacity, READ_LIMIT, Deadline::Queued, true)
    }
    /// For files with their own schema owner, such as the memory file.
    pub fn open_unchecked(
        path: &Path,
        threads: usize,
        capacity: usize,
        limit: Duration,
    ) -> Result<Self, String> {
        Self::open_unchecked_deadline(path, threads, capacity, limit, Deadline::Queued)
    }
    /// `open_unchecked` with an explicit deadline policy. The memory pool asks
    /// for `Deadline::Started`, so a job's run budget does not shrink by its
    /// queue wait.
    pub fn open_unchecked_deadline(
        path: &Path,
        threads: usize,
        capacity: usize,
        limit: Duration,
        deadline: Deadline,
    ) -> Result<Self, String> {
        Self::build(path, threads, capacity, limit, deadline, false)
    }
    fn build(
        path: &Path,
        threads: usize,
        capacity: usize,
        limit: Duration,
        deadline: Deadline,
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
            deadline,
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
        let deadline = self.deadline;
        let queued = Instant::now();
        let checked = self.checked;
        let stats = self.stats.clone();
        let counted = stats.clone();
        let job = Box::new(move |c: &mut Connection, cookie: &mut Option<(i64, i64)>| {
            // The caller waits at most `limit` for a reader, so a job that
            // starts after that has nobody left to answer.
            if queued.elapsed() >= limit {
                stats.aged();
                let _ = reply.send(Err("reader-queue-full".into()));
                return;
            }
            // A queue-relative deadline counts from when the caller queued the
            // job, so a caller that waits at most `limit` is never followed by
            // up to `limit` more seconds of work no one will read. A
            // start-relative one leaves the full run budget however long the
            // queue wait was.
            let deadline = match deadline {
                Deadline::Queued => queued + limit,
                Deadline::Started => Instant::now() + limit,
            };
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
            .map_err(|_| {
                // A full queue turned the job away before any reader saw it.
                counted.full();
                "reader-queue-full"
            })?
            .map_err(|_| "reader-stopped")?;
        let remaining = limit.saturating_sub(queued.elapsed());
        tokio::time::timeout(remaining, starting)
            .await
            .map_err(|_| "reader-queue-full")?
            .map_err(|_| "reader-stopped")?;
        let answer = match deadline {
            // A job that started just before the deadline still owes an answer
            // by then. The runner refuses at the same instant, so either side
            // reports the same reason.
            Deadline::Queued => {
                let remaining = limit.saturating_sub(queued.elapsed());
                tokio::time::timeout(remaining, receive)
                    .await
                    .map_err(|_| "reader-timeout")?
            }
            // The run budget counts from the start, so the caller waits it out.
            Deadline::Started => receive.await,
        };
        answer.map_err(|_| "reader-stopped")?
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
        // A shared pool would answer `reader-queue-full` when the caller's
        // budget runs out. The lookup either runs now or not at all.
        lookups
            .submit(|_| Ok(()))
            .await
            .expect("a held handler pool must not delay a credential lookup");
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
            Arc::new(ReaderPool::open_unchecked(&path, 1, 8, Duration::from_millis(600)).unwrap());
        let probe = pool.queue_probe();
        let (hold, gate, entered) = hold_reader(&pool);
        entered.recv_timeout(Duration::from_secs(10)).unwrap();
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
        // The job must be in the queue before the clock starts, or a fast
        // reader could pick it up inside its deadline.
        wait_for("the late job is queued", || probe() == 1);
        // Longer than the caller's 600 ms budget, shorter than the old 2 s one.
        tokio::time::sleep(Duration::from_millis(800)).await;
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
        let limit = Duration::from_secs(2);
        let pool = Arc::new(ReaderPool::open_unchecked(&path, 1, 8, limit).unwrap());
        let probe = pool.queue_probe();
        let (hold, gate, entered) = hold_reader(&pool);
        entered.recv_timeout(Duration::from_secs(10)).unwrap();
        let pool_copy = pool.clone();
        let slow = tokio::spawn(async move {
            pool_copy
                .submit(|_| {
                    // A start-relative 2 s deadline would still allow this; the
                    // queue-relative one leaves only 1 s of the budget.
                    std::thread::sleep(Duration::from_millis(1500));
                    Ok(())
                })
                .await
        });
        wait_for("the slow job is queued", || probe() == 1);
        // Half the budget passes in the queue before a reader is free.
        tokio::time::sleep(Duration::from_secs(1)).await;
        gate.send(()).unwrap();
        hold.await.unwrap();
        assert_eq!(
            slow.await.unwrap().err().as_deref(),
            Some("reader-timeout"),
            "work past the queued deadline must not answer the caller"
        );
        drop(pool);
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_memory_job_that_queued_still_gets_its_full_run_budget() {
        let path = temp_db("memory-budget");
        // The memory pool's own constructor and limit, as `memory::Store::open`
        // opens it: the run budget counts from when a reader starts the job.
        let limit = Duration::from_secs(4);
        let pool = Arc::new(
            ReaderPool::open_unchecked_deadline(&path, 1, 8, limit, Deadline::Started).unwrap(),
        );
        let probe = pool.queue_probe();
        let (hold, gate, entered) = hold_reader(&pool);
        entered.recv_timeout(Duration::from_secs(10)).unwrap();
        let finished = Arc::new(AtomicUsize::new(0));
        let marker = finished.clone();
        let pool_copy = pool.clone();
        let queued = tokio::spawn(async move {
            pool_copy
                .submit(move |_| {
                    // Longer than the 1.5 s a queue-relative deadline would
                    // leave, shorter than the full 4 s run budget.
                    std::thread::sleep(Duration::from_secs(2));
                    marker.fetch_add(1, Ordering::SeqCst);
                    Ok::<_, rusqlite::Error>(())
                })
                .await
        });
        // Most of the limit passes in the queue before a reader is free.
        wait_for("the memory job is queued", || probe() == 1);
        tokio::time::sleep(Duration::from_millis(2500)).await;
        gate.send(()).unwrap();
        hold.await.unwrap();
        assert_eq!(
            queued.await.unwrap(),
            Ok(()),
            "a memory read that queued for most of its limit still owes a full run budget"
        );
        assert_eq!(finished.load(Ordering::SeqCst), 1);
        drop(pool);
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_full_queue_refusal_is_counted() {
        let path = temp_db("full");
        let limit = Duration::from_millis(300);
        let pool = Arc::new(ReaderPool::open_unchecked(&path, 1, 1, limit).unwrap());
        let probe = pool.queue_probe();
        let (hold, gate, entered) = hold_reader(&pool);
        entered.recv_timeout(Duration::from_secs(10)).unwrap();
        // One job fills the queue of one while the only reader is held.
        let pool_copy = pool.clone();
        let queued = tokio::spawn(async move { pool_copy.submit(|_| Ok(())).await });
        wait_for("the queue is full", || probe() == 1);
        // The next job cannot even enter the queue, so no reader ever sees it.
        assert_eq!(
            pool.submit(|_| Ok(())).await.err().as_deref(),
            Some("reader-queue-full")
        );
        assert_eq!(pool.stats().snapshot().full, 1);
        gate.send(()).unwrap();
        let _ = hold.await;
        let _ = queued.await;
        drop(pool);
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn reader_stats_count_jobs_and_their_time() {
        let path = temp_db("stats");
        let pool = ReaderPool::open_unchecked(&path, 1, 8, READ_LIMIT).unwrap();
        pool.submit(|_| {
            // A job that returns instantly can round its run time down to 0.
            std::thread::sleep(Duration::from_millis(2));
            Ok(7)
        })
        .await
        .unwrap();
        let snapshot = pool.stats().snapshot();
        assert_eq!(snapshot.jobs, 1);
        assert!(
            snapshot.run_micros >= 1_000,
            "a job records its run time: {}",
            snapshot.run_micros
        );
        assert_eq!(snapshot.aged, 0);
        assert_eq!(snapshot.expired, 0);
        assert_eq!(snapshot.abandoned, 0);
        drop(pool);
        let _ = std::fs::remove_file(&path);
    }
}
