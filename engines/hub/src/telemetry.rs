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
    /// One probe per reader pool. `reader_queue_depth` is their sum.
    pub fn attach_readers(&self, probe: QueueProbe) {
        self.readers.lock().unwrap().push(probe);
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
        text
    }
}
pub struct RequestLog {
    pub telemetry: std::sync::Arc<Telemetry>,
    pub id: u64,
    pub family: &'static str,
    pub status: u16,
    pub credential_kind: &'static str,
    pub request_bytes: std::sync::Arc<AtomicU64>,
    pub response_bytes: u64,
    pub start: Instant,
}
impl Drop for RequestLog {
    fn drop(&mut self) {
        let seconds = self.start.elapsed().as_secs_f64();
        self.telemetry.request(self.family, self.status, seconds);
        eprintln!(
            "{}",
            serde_json::json!({"event":"request","request_id":self.id,"family":self.family,"status":self.status,"latency_seconds":seconds,"credential_kind":self.credential_kind,"request_bytes":self.request_bytes.load(Ordering::Relaxed),"response_bytes":self.response_bytes})
        );
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
        assert!(!text.contains("Not yet wired"));
        assert!(text.contains("# HELP brigade_hub_writer_queue_depth No writer in this process"));
        assert!(text.contains("# HELP brigade_hub_reader_queue_depth No reader pool attached"));
        assert!(text.contains("# HELP brigade_hub_cpu_queue_depth No CPU pool attached"));
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

    #[tokio::test(flavor = "multi_thread")]
    async fn reader_and_cpu_depth_follow_queued_jobs() {
        let path = std::env::temp_dir().join(format!("hub-telemetry-{}.db", std::process::id()));
        rusqlite::Connection::open(&path)
            .unwrap()
            .execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE t(x);")
            .unwrap();
        let readers =
            Arc::new(ReaderPool::open_unchecked(&path, 1, 8, Duration::from_secs(5)).unwrap());
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
