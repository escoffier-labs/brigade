//! Bounded labels only. No request path, identity, token or body is retained.
use std::{
    collections::BTreeMap,
    sync::{
        Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::Instant,
};
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
}
impl Telemetry {
    pub fn id(&self) -> u64 {
        self.sequence.fetch_add(1, Ordering::Relaxed) + 1
    }
    pub fn shadow(&self, result: &'static str) {
        *self.shadow.lock().unwrap().entry(result).or_default() += 1;
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
        // S4a owns these values. NaN explicitly means unavailable, never idle.
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
            text += &format!("# HELP brigade_hub_{name} Not yet wired.\nbrigade_hub_{name} NaN\n");
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
