//! Bounded labels only. No request path, identity, token or body is retained.
use crate::writer::WriterStats;
use std::{
    collections::BTreeMap,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::Instant,
};
use tokio::sync::mpsc;
/// Reads how many jobs wait in one bounded queue. It holds only a weak sender,
/// so a scrape never keeps a pool's workers alive and reads 0 after the pool stops.
pub type QueueProbe = Box<dyn Fn() -> usize + Send + Sync>;
pub fn queue_probe<T: Send + 'static>(sender: &mpsc::Sender<T>) -> QueueProbe {
    let weak = sender.downgrade();
    Box::new(move || {
        weak.upgrade()
            .map_or(0, |s| s.max_capacity() - s.capacity())
    })
}
/// One reader pool's counters. `Telemetry::attach_reader_stats` keeps them per
/// pool, so every series carries the pool it came from.
#[derive(Default)]
pub struct ReaderStats {
    jobs: AtomicU64,
    queue_micros: AtomicU64,
    run_micros: AtomicU64,
    aged: AtomicU64,
    expired: AtomicU64,
    abandoned: AtomicU64,
    full: AtomicU64,
}
/// The reader pools a hub process can run, as the `pool` label spells them.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Pool {
    Handler,
    Lookup,
    Memory,
}
impl Pool {
    pub const ALL: [Self; 3] = [Self::Handler, Self::Lookup, Self::Memory];
    pub fn label(self) -> &'static str {
        match self {
            Self::Handler => "handler",
            Self::Lookup => "lookup",
            Self::Memory => "memory",
        }
    }
}
#[derive(Clone, Copy, Default, PartialEq, Eq)]
pub struct ReaderSnapshot {
    pub jobs: u64,
    pub queue_micros: u64,
    pub run_micros: u64,
    pub aged: u64,
    pub expired: u64,
    pub abandoned: u64,
    pub full: u64,
}
fn micros(span: std::time::Duration) -> u64 {
    u64::try_from(span.as_micros()).unwrap_or(u64::MAX)
}
impl ReaderStats {
    /// A reader picked the job up after waiting this long in the queue.
    pub fn ran(&self, waited: std::time::Duration) {
        self.jobs.fetch_add(1, Ordering::Relaxed);
        self.queue_micros
            .fetch_add(micros(waited), Ordering::Relaxed);
    }
    pub fn finished(&self, spent: std::time::Duration) {
        self.run_micros.fetch_add(micros(spent), Ordering::Relaxed);
    }
    /// The job aged past its deadline in the queue and never ran its query.
    pub fn aged(&self) {
        self.aged.fetch_add(1, Ordering::Relaxed);
    }
    /// The job ran past the deadline it was queued with.
    pub fn expired(&self) {
        self.expired.fetch_add(1, Ordering::Relaxed);
    }
    /// The caller dropped the reply channel before the job ran.
    pub fn abandoned(&self) {
        self.abandoned.fetch_add(1, Ordering::Relaxed);
    }
    /// A full queue refused the job before any reader could pick it up.
    pub fn full(&self) {
        self.full.fetch_add(1, Ordering::Relaxed);
    }
    pub fn snapshot(&self) -> ReaderSnapshot {
        ReaderSnapshot {
            jobs: self.jobs.load(Ordering::Relaxed),
            queue_micros: self.queue_micros.load(Ordering::Relaxed),
            run_micros: self.run_micros.load(Ordering::Relaxed),
            aged: self.aged.load(Ordering::Relaxed),
            expired: self.expired.load(Ordering::Relaxed),
            abandoned: self.abandoned.load(Ordering::Relaxed),
            full: self.full.load(Ordering::Relaxed),
        }
    }
}
#[derive(Default)]
struct RequestStats {
    count: u64,
    seconds: f64,
    buckets: [u64; 6],
}
#[derive(Default)]
pub struct Telemetry {
    pub sequence: AtomicU64,
    requests: Mutex<BTreeMap<(&'static str, u16), RequestStats>>,
    shadow: Mutex<BTreeMap<&'static str, u64>>,
    writer: Mutex<Option<Arc<WriterStats>>>,
    readers: Mutex<Vec<QueueProbe>>,
    reader_stats: Mutex<BTreeMap<Pool, Arc<ReaderStats>>>,
    cpu: Mutex<Vec<QueueProbe>>,
}
impl Telemetry {
    pub fn id(&self) -> u64 {
        self.sequence.fetch_add(1, Ordering::Relaxed) + 1
    }
    pub fn shadow(&self, result: &'static str) {
        *self.shadow.lock().unwrap().entry(result).or_default() += 1;
    }
    /// Call once when this process starts a writer. Without it the writer
    /// metrics render as NaN, since a pure proxy front has no writer of its own.
    pub fn attach_writer(&self, stats: Arc<WriterStats>) {
        *self.writer.lock().unwrap() = Some(stats);
    }
    /// One probe per reader pool. `reader_queue_depth` is their sum, as it was
    /// before the pools split: the handler and memory pools. The lookup pool is
    /// counted per pool by `attach_reader_stats` instead, so this gauge keeps
    /// reporting what it reported on main.
    pub fn attach_readers(&self, probe: QueueProbe) {
        self.readers.lock().unwrap().push(probe);
    }
    /// One call per reader pool, with `attach_readers`, which names the pool
    /// every counter carries.
    pub fn attach_reader_stats(&self, pool: Pool, stats: Arc<ReaderStats>) {
        self.reader_stats.lock().unwrap().insert(pool, stats);
    }
    pub fn attach_cpu(&self, probe: QueueProbe) {
        self.cpu.lock().unwrap().push(probe);
    }
    pub fn request(&self, family: &'static str, status: u16, seconds: f64) {
        let mut all = self.requests.lock().unwrap();
        let v = all.entry((family, status / 100)).or_default();
        v.count += 1;
        v.seconds += seconds;
        for (bucket, limit) in
            v.buckets
                .iter_mut()
                .zip([0.001, 0.01, 0.1, 1.0, 10.0, f64::INFINITY])
        {
            if seconds <= limit {
                *bucket += 1;
            }
        }
    }
    pub fn render(&self) -> String {
        let mut text = String::new();
        for ((family, status), stats) in self.requests.lock().unwrap().iter() {
            let count = stats.count;
            let seconds = stats.seconds;
            for (bucket, limit) in stats
                .buckets
                .iter()
                .zip(["0.001", "0.01", "0.1", "1", "10", "+Inf"])
            {
                text += &format!(
                    "brigade_hub_request_duration_seconds_bucket{{family=\"{family}\",status_class=\"{status}xx\",le=\"{limit}\"}} {bucket}\n"
                );
            }
            text += &format!(
                "brigade_hub_requests_total{{family=\"{family}\",status_class=\"{status}xx\"}} {count}\nbrigade_hub_request_duration_seconds_sum{{family=\"{family}\",status_class=\"{status}xx\"}} {seconds}\nbrigade_hub_request_duration_seconds_count{{family=\"{family}\",status_class=\"{status}xx\"}} {count}\n"
            );
        }
        for (result, count) in self.shadow.lock().unwrap().iter() {
            text += &format!(
                "brigade_hub_shadow_comparisons_total{{family=\"health\",result=\"{result}\"}} {count}\n"
            );
        }
        // NaN means unavailable, never idle.
        let writer = self.writer.lock().unwrap().as_ref().map(|w| w.snapshot());
        let none = "No writer in this process: the front proxies to the upstream hub, which commits all writes.";
        let fields: [(&str, &str, Option<f64>); 6] = [
            (
                "writer_queue_depth",
                "Commands waiting for the writer thread (gauge).",
                writer.map(|w| w.queue_depth as f64),
            ),
            (
                "writer_batch_size",
                "Commands in the last batch the writer finished (gauge).",
                writer.map(|w| w.batch_size as f64),
            ),
            (
                "writer_apply_seconds",
                "Apply time of the last batch, re-applies included (gauge).",
                writer.map(|w| w.apply_seconds),
            ),
            (
                "writer_commit_seconds",
                "COMMIT time of the last batch, 0 when none was tried (gauge).",
                writer.map(|w| w.commit_seconds),
            ),
            (
                "writer_lock_wait_seconds",
                "Wait for the arbiter turn and the hub write lock before the last batch, also when the wait failed (gauge).",
                writer.map(|w| w.lock_wait_seconds),
            ),
            (
                "writer_aborted_batches_total",
                "Batches that hit a whole-transaction abort or a failed COMMIT (counter).",
                writer.map(|w| w.aborted_batches as f64),
            ),
        ];
        for (name, help, value) in fields {
            text += &match value {
                Some(v) => format!("# HELP brigade_hub_{name} {help}\nbrigade_hub_{name} {v}\n"),
                None => format!("# HELP brigade_hub_{name} {none}\nbrigade_hub_{name} NaN\n"),
            };
        }
        if let Some(writer) = self.writer.lock().unwrap().as_ref() {
            let (bounds, counts) = writer.commit_histogram();
            text += "# HELP brigade_hub_writer_commit_duration_seconds Every FULL COMMIT attempt, failures included.\n# TYPE brigade_hub_writer_commit_duration_seconds histogram\n";
            let mut total = 0;
            for (bound, count) in bounds.into_iter().zip(counts) {
                total += count;
                let bound = if bound.is_infinite() {
                    "+Inf".into()
                } else {
                    bound.to_string()
                };
                text += &format!(
                    "brigade_hub_writer_commit_duration_seconds_bucket{{le=\"{bound}\"}} {total}\n"
                );
            }
            text += &format!(
                "brigade_hub_writer_commit_duration_seconds_count {total}\nbrigade_hub_writer_commit_duration_seconds_sum {}\n",
                writer.commit_sum_seconds()
            );
        }
        for (name, help, missing, probes) in [
            (
                "reader_queue_depth",
                "Read jobs waiting for a reader thread, all reader pools (gauge).",
                "No reader pool attached to this process.",
                &self.readers,
            ),
            (
                "cpu_queue_depth",
                "Jobs waiting for the CPU pool (gauge).",
                "No CPU pool attached to this process.",
                &self.cpu,
            ),
        ] {
            let probes = probes.lock().unwrap();
            text += &if probes.is_empty() {
                format!("# HELP brigade_hub_{name} {missing}\nbrigade_hub_{name} NaN\n")
            } else {
                let depth: usize = probes.iter().map(|probe| probe()).sum();
                format!("# HELP brigade_hub_{name} {help}\nbrigade_hub_{name} {depth}\n")
            };
        }
        let snapshots = self.reader_stats.lock().unwrap().clone();
        let missing = "No reader pool attached to this process.";
        for (name, help, field, time) in [
            (
                "reader_jobs_total",
                "Jobs a reader ran a query for, by pool (counter).",
                (|s: &ReaderSnapshot| s.jobs) as fn(&ReaderSnapshot) -> u64,
                false,
            ),
            (
                "reader_queue_wait_seconds_total",
                "Time jobs waited for a reader, by pool (counter).",
                |s: &ReaderSnapshot| s.queue_micros,
                true,
            ),
            (
                "reader_run_seconds_total",
                "Time readers spent running jobs, by pool (counter).",
                |s: &ReaderSnapshot| s.run_micros,
                true,
            ),
        ] {
            text += &if snapshots.is_empty() {
                format!("# HELP brigade_hub_{name} {missing}\n")
            } else {
                format!("# HELP brigade_hub_{name} {help}\n")
            };
            for pool in Pool::ALL {
                let value = match snapshots.get(&pool) {
                    Some(stats) => {
                        let raw = field(&stats.snapshot());
                        if time { raw as f64 / 1e6 } else { raw as f64 }
                    }
                    None => f64::NAN,
                };
                text += &format!("brigade_hub_{name}{{pool=\"{}\"}} {value}\n", pool.label());
            }
        }
        text += &if snapshots.is_empty() {
            format!("# HELP brigade_hub_reader_timeouts_total {missing}\n")
        } else {
            "# HELP brigade_hub_reader_timeouts_total Read jobs that hit a deadline, by cause and pool (counter).\n".into()
        };
        for (reason, field) in [
            (
                "aged-in-queue",
                (|s: &ReaderSnapshot| s.aged) as fn(&ReaderSnapshot) -> u64,
            ),
            ("run-deadline", |s: &ReaderSnapshot| s.expired),
            ("caller-gone", |s: &ReaderSnapshot| s.abandoned),
            // A full queue refused the job before any reader saw it.
            ("queue-full", |s: &ReaderSnapshot| s.full),
        ] {
            for pool in Pool::ALL {
                let value = match snapshots.get(&pool) {
                    Some(stats) => field(&stats.snapshot()) as f64,
                    None => f64::NAN,
                };
                text += &format!(
                    "brigade_hub_reader_timeouts_total{{pool=\"{}\",reason=\"{reason}\"}} {value}\n",
                    pool.label()
                );
            }
        }
        text
    }
}
pub struct RequestLog {
    pub telemetry: std::sync::Arc<Telemetry>,
    pub id: u64,
    pub family: &'static str,
    pub status: u16,
    pub credential_kind: &'static str,
    /// The phase the request had reached. A cancelled request keeps the phase
    /// that stalled, which its status and credential label alone cannot show.
    pub stage: StageSlot,
    pub request_bytes: std::sync::Arc<AtomicU64>,
    pub response_bytes: u64,
    pub start: Instant,
}
/// Where a request was when its log line was written. Labels are bounded strings
/// and carry no request data.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum Stage {
    /// Admitted by the front; no credential lookup has started.
    Received,
    /// Inside the credential lookup pool.
    CredentialLookup,
    /// Authenticated, in admission or a native handler.
    Handler,
    /// Waiting on a native write's commit. The routes that share the hub
    /// writer set it: registration, claims, messages and heartbeat. Memory
    /// writes go through the memory store's own writer and stay `handler`.
    Writer,
    /// Waiting on, or streaming from, the Python upstream.
    Upstream,
    /// The reply is built; its body is streaming to the client.
    Replying,
}
impl Stage {
    pub fn label(self) -> &'static str {
        match self {
            Self::Received => "received",
            Self::CredentialLookup => "credential-lookup",
            Self::Handler => "handler",
            Self::Writer => "writer",
            Self::Upstream => "upstream",
            Self::Replying => "replying",
        }
    }
    fn code(self) -> u8 {
        self as u8
    }
    fn from_code(code: u8) -> Self {
        match code {
            1 => Self::CredentialLookup,
            2 => Self::Handler,
            3 => Self::Writer,
            4 => Self::Upstream,
            5 => Self::Replying,
            _ => Self::Received,
        }
    }
}
/// The stage slot a request carries through its phases. Clones share it.
#[derive(Clone, Debug, Default)]
pub struct StageSlot(std::sync::Arc<std::sync::atomic::AtomicU8>);
impl StageSlot {
    pub fn set(&self, stage: Stage) {
        self.0.store(stage.code(), Ordering::Relaxed);
    }
    pub fn get(&self) -> Stage {
        Stage::from_code(self.0.load(Ordering::Relaxed))
    }
}
/// Set the stage on the slot the front attached to this request, if any.
pub fn mark(parts: &axum::http::request::Parts, stage: Stage) {
    if let Some(slot) = parts.extensions.get::<StageSlot>() {
        slot.set(stage);
    }
}
impl RequestLog {
    /// The one request line this log writes, before it is emitted.
    pub fn line(&self) -> serde_json::Value {
        serde_json::json!({"event":"request","request_id":self.id,"family":self.family,"status":self.status,"latency_seconds":self.start.elapsed().as_secs_f64(),"credential_kind":self.credential_kind,"stage":self.stage.get().label(),"request_bytes":self.request_bytes.load(Ordering::Relaxed),"response_bytes":self.response_bytes})
    }
}
impl Drop for RequestLog {
    fn drop(&mut self) {
        let line = self.line();
        // The histogram reads the same latency the line carries, so the metric
        // and the log never disagree.
        self.telemetry.request(
            self.family,
            self.status,
            line["latency_seconds"].as_f64().unwrap_or_default(),
        );
        eprintln!("{line}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::read::{CpuPool, ReaderPool};
    use std::{sync::mpsc as std_mpsc, time::Duration};

    fn value(text: &str, name: &str) -> String {
        let prefix = format!("brigade_hub_{name} ");
        text.lines()
            .find_map(|line| line.strip_prefix(&prefix))
            .unwrap_or_else(|| panic!("{name} missing"))
            .to_owned()
    }
    fn wait_for(what: &str, mut done: impl FnMut() -> bool) {
        let deadline = Instant::now() + Duration::from_secs(5);
        while !done() {
            assert!(Instant::now() < deadline, "{what}");
            std::thread::sleep(Duration::from_millis(5));
        }
    }

    /// One labelled series, e.g. `reader_jobs_total{pool="handler"}`.
    fn series(text: &str, name: &str, labels: &str) -> String {
        let prefix = format!("brigade_hub_{name}{{{labels}}} ");
        text.lines()
            .find_map(|line| line.strip_prefix(&prefix))
            .unwrap_or_else(|| panic!("{name}{{{labels}}} missing"))
            .to_owned()
    }

    /// The request line names the phase, and a cancelled request keeps the
    /// phase that stalled rather than the label it started with.
    #[test]
    fn request_line_names_the_stage_it_reached() {
        let slot = StageSlot::default();
        let log = RequestLog {
            telemetry: Arc::new(Telemetry::default()),
            id: 7,
            family: "status",
            status: 500,
            credential_kind: "unchecked",
            stage: slot.clone(),
            request_bytes: Arc::default(),
            response_bytes: 0,
            start: Instant::now(),
        };
        assert_eq!(log.line()["stage"], "received");
        for (stage, label) in [
            (Stage::CredentialLookup, "credential-lookup"),
            (Stage::Handler, "handler"),
            (Stage::Writer, "writer"),
            (Stage::Upstream, "upstream"),
            (Stage::Replying, "replying"),
        ] {
            slot.set(stage);
            assert_eq!(log.line()["stage"], label);
            assert_eq!(slot.get(), stage);
        }
        // The earlier fields keep their meaning and stay on the line.
        let line = log.line();
        assert_eq!(line["credential_kind"], "unchecked");
        assert_eq!(line["family"], "status");
        assert_eq!(line["status"], 500);
    }

    #[test]
    fn unattached_pools_render_nan_with_the_reason() {
        let text = Telemetry::default().render();
        for name in [
            "writer_queue_depth",
            "writer_batch_size",
            "writer_apply_seconds",
            "writer_commit_seconds",
            "writer_lock_wait_seconds",
            "writer_aborted_batches_total",
            "reader_queue_depth",
            "cpu_queue_depth",
        ] {
            assert_eq!(value(&text, name), "NaN", "{name}");
        }
        for name in [
            "reader_jobs_total",
            "reader_queue_wait_seconds_total",
            "reader_run_seconds_total",
        ] {
            for pool in Pool::ALL {
                assert_eq!(
                    series(&text, name, &format!("pool=\"{}\"", pool.label())),
                    "NaN",
                    "{name} {}",
                    pool.label()
                );
            }
        }
        for reason in ["aged-in-queue", "run-deadline", "caller-gone", "queue-full"] {
            assert!(text.contains(&format!(
                "brigade_hub_reader_timeouts_total{{pool=\"handler\",reason=\"{reason}\"}} NaN"
            )));
        }
        assert!(!text.contains("Not yet wired"));
        assert!(text.contains("# HELP brigade_hub_writer_queue_depth No writer in this process"));
        assert!(text.contains("# HELP brigade_hub_reader_queue_depth No reader pool attached"));
        assert!(text.contains("# HELP brigade_hub_cpu_queue_depth No CPU pool attached"));
        assert!(text.contains("# HELP brigade_hub_reader_jobs_total No reader pool attached"));
    }

    #[test]
    fn reader_probes_sum_across_pools() {
        let telemetry = Telemetry::default();
        telemetry.attach_readers(Box::new(|| 2));
        telemetry.attach_readers(Box::new(|| 3));
        telemetry.attach_cpu(Box::new(|| 0));
        let text = telemetry.render();
        assert_eq!(value(&text, "reader_queue_depth"), "5");
        assert_eq!(value(&text, "cpu_queue_depth"), "0");
    }

    #[test]
    fn reader_stats_keep_each_pool_apart_and_split_timeouts_by_cause() {
        let telemetry = Telemetry::default();
        let handlers = Arc::new(ReaderStats::default());
        let lookups = Arc::new(ReaderStats::default());
        telemetry.attach_reader_stats(Pool::Handler, handlers.clone());
        telemetry.attach_reader_stats(Pool::Lookup, lookups.clone());
        handlers.ran(Duration::from_millis(20));
        handlers.finished(Duration::from_millis(30));
        handlers.aged();
        handlers.expired();
        handlers.full();
        lookups.ran(Duration::from_millis(0));
        lookups.abandoned();
        let text = telemetry.render();
        assert_eq!(series(&text, "reader_jobs_total", "pool=\"handler\""), "1");
        assert_eq!(series(&text, "reader_jobs_total", "pool=\"lookup\""), "1");
        // An unattached pool is unavailable, never idle.
        assert_eq!(series(&text, "reader_jobs_total", "pool=\"memory\""), "NaN");
        assert_eq!(
            series(&text, "reader_queue_wait_seconds_total", "pool=\"handler\""),
            "0.02"
        );
        assert_eq!(
            series(&text, "reader_run_seconds_total", "pool=\"handler\""),
            "0.03"
        );
        assert!(text.contains(
            "brigade_hub_reader_timeouts_total{pool=\"handler\",reason=\"aged-in-queue\"} 1"
        ));
        assert!(text.contains(
            "brigade_hub_reader_timeouts_total{pool=\"handler\",reason=\"run-deadline\"} 1"
        ));
        assert!(text.contains(
            "brigade_hub_reader_timeouts_total{pool=\"handler\",reason=\"queue-full\"} 1"
        ));
        assert!(text.contains(
            "brigade_hub_reader_timeouts_total{pool=\"lookup\",reason=\"caller-gone\"} 1"
        ));
        assert!(text.contains(
            "brigade_hub_reader_timeouts_total{pool=\"lookup\",reason=\"queue-full\"} 0"
        ));
        assert!(text.contains(
            "brigade_hub_reader_timeouts_total{pool=\"memory\",reason=\"queue-full\"} NaN"
        ));
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn reader_and_cpu_depth_follow_queued_jobs() {
        let path = std::env::temp_dir().join(format!("hub-telemetry-{}.db", std::process::id()));
        rusqlite::Connection::open(&path)
            .unwrap()
            .execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE t(x);")
            .unwrap();
        // A pool limit far past any slow-runner delay: the held and queued jobs
        // here must never age out of the queue, which would turn the queue
        // depth this test reads into a refusal.
        let readers =
            Arc::new(ReaderPool::open_unchecked(&path, 1, 8, Duration::from_secs(120)).unwrap());
        let cpu = Arc::new(CpuPool::new(1, 8));
        let telemetry = Telemetry::default();
        telemetry.attach_readers(readers.queue_probe());
        telemetry.attach_cpu(cpu.queue_probe());
        assert_eq!(value(&telemetry.render(), "reader_queue_depth"), "0");
        assert_eq!(value(&telemetry.render(), "cpu_queue_depth"), "0");

        // One thread per pool is held by a blocked job, so three more must queue.
        let (hold_reader, reader_gate) = std_mpsc::channel::<()>();
        let (reader_busy, reader_started) = std_mpsc::channel::<()>();
        let (hold_cpu, cpu_gate) = std_mpsc::channel::<()>();
        let (cpu_busy, cpu_started) = std_mpsc::channel::<()>();
        let mut jobs = Vec::new();
        let pool = readers.clone();
        jobs.push(tokio::spawn(async move {
            pool.submit(move |_| {
                reader_busy.send(()).unwrap();
                reader_gate.recv().ok();
                Ok(())
            })
            .await
            .unwrap();
        }));
        let pool = cpu.clone();
        jobs.push(tokio::spawn(async move {
            pool.submit(move || {
                cpu_busy.send(()).unwrap();
                cpu_gate.recv().ok();
            })
            .await
            .unwrap();
        }));
        reader_started.recv_timeout(Duration::from_secs(5)).unwrap();
        cpu_started.recv_timeout(Duration::from_secs(5)).unwrap();
        for _ in 0..3 {
            let pool = readers.clone();
            jobs.push(tokio::spawn(async move {
                pool.submit(|_| Ok(())).await.unwrap();
            }));
            let pool = cpu.clone();
            jobs.push(tokio::spawn(async move {
                pool.submit(|| ()).await.unwrap();
            }));
        }
        wait_for("both queues reach 3", || {
            let text = telemetry.render();
            value(&text, "reader_queue_depth") == "3" && value(&text, "cpu_queue_depth") == "3"
        });
        hold_reader.send(()).unwrap();
        hold_cpu.send(()).unwrap();
        for job in jobs {
            job.await.unwrap();
        }
        let text = telemetry.render();
        assert_eq!(value(&text, "reader_queue_depth"), "0");
        assert_eq!(value(&text, "cpu_queue_depth"), "0");
        // A stopped pool reads as empty, and the probe does not keep it alive.
        let probe = cpu.queue_probe();
        drop(cpu);
        assert_eq!(probe(), 0);
        let _ = std::fs::remove_file(&path);
    }
}
