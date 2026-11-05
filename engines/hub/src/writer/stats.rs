//! Lock-free writer observations for `/metrics`. Only the writer thread stores,
//! scrapes only load, and every store is one relaxed atomic write per batch,
//! never per command. The gauges hold the last batch the thread finished.
use crate::telemetry::QueueProbe;
use std::{
    sync::atomic::{AtomicU64, Ordering::Relaxed},
    time::Duration,
};

const COMMIT_BOUNDS: [f64; 21] = [
    0.0001,
    0.00025,
    0.0005,
    0.001,
    0.002,
    0.003,
    0.005,
    0.01,
    0.02,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.0,
    3.0,
    5.0,
    10.0,
    30.0,
    f64::INFINITY,
];

#[derive(Default)]
pub struct WriterStats {
    queue: Option<QueueProbe>,
    batch_size: AtomicU64,
    apply_nanos: AtomicU64,
    commit_nanos: AtomicU64,
    commit_sum_nanos: AtomicU64,
    lock_wait_nanos: AtomicU64,
    aborted_batches: AtomicU64,
    commit_buckets: [AtomicU64; 21],
}
/// One scrape. The gauges describe the same most recent batch only loosely: each
/// field is loaded on its own, which is enough for a sampled metric.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct WriterSnapshot {
    pub queue_depth: usize,
    pub batch_size: u64,
    pub apply_seconds: f64,
    pub commit_seconds: f64,
    pub lock_wait_seconds: f64,
    pub aborted_batches: u64,
}
fn nanos(d: Duration) -> u64 {
    u64::try_from(d.as_nanos()).unwrap_or(u64::MAX)
}
fn seconds(n: &AtomicU64) -> f64 {
    n.load(Relaxed) as f64 / 1e9
}
impl WriterStats {
    pub(super) fn with_queue(queue: QueueProbe) -> Self {
        Self {
            queue: Some(queue),
            ..Self::default()
        }
    }
    /// Time from taking a command off the queue to holding the hub write lock,
    /// the arbiter turn and the BEGIN retries included. Recorded when the lock
    /// is never won as well, since that wait is the one B-M6 needs to see.
    pub(super) fn lock_wait(&self, waited: Duration) {
        self.lock_wait_nanos.store(nanos(waited), Relaxed);
    }
    /// A batch that reached BEGIN. `commit` is `None` when no COMMIT was tried.
    pub(super) fn batch(
        &self,
        size: usize,
        apply: Duration,
        commit: Option<Duration>,
        aborted: bool,
    ) {
        self.batch_size.store(size as u64, Relaxed);
        self.apply_nanos.store(nanos(apply), Relaxed);
        self.commit_nanos.store(commit.map_or(0, nanos), Relaxed);
        if let Some(commit) = commit {
            self.commit_sum_nanos.fetch_add(nanos(commit), Relaxed);
            let bucket = COMMIT_BOUNDS
                .iter()
                .position(|b| commit.as_secs_f64() <= *b)
                .unwrap();
            self.commit_buckets[bucket].fetch_add(1, Relaxed);
        }
        if aborted {
            self.aborted_batches.fetch_add(1, Relaxed);
        }
    }
    /// Exclusive buckets count every COMMIT attempt, including failed commits.
    /// Scrapes can subtract cumulative buckets to bound p99 over a run window.
    pub fn commit_histogram(&self) -> ([f64; 21], [u64; 21]) {
        (
            COMMIT_BOUNDS,
            std::array::from_fn(|i| self.commit_buckets[i].load(Relaxed)),
        )
    }
    pub fn commit_sum_seconds(&self) -> f64 {
        seconds(&self.commit_sum_nanos)
    }
    pub fn snapshot(&self) -> WriterSnapshot {
        WriterSnapshot {
            queue_depth: self.queue.as_ref().map_or(0, |depth| depth()),
            batch_size: self.batch_size.load(Relaxed),
            apply_seconds: seconds(&self.apply_nanos),
            commit_seconds: seconds(&self.commit_nanos),
            lock_wait_seconds: seconds(&self.lock_wait_nanos),
            aborted_batches: self.aborted_batches.load(Relaxed),
        }
    }
}

#[cfg(test)]
mod commit_histogram_tests {
    use super::*;
    #[test]
    fn every_attempted_commit_is_counted_once_including_failures() {
        let stats = WriterStats::default();
        stats.batch(1, Duration::ZERO, Some(Duration::from_millis(2)), false);
        stats.batch(1, Duration::ZERO, Some(Duration::from_millis(21)), true);
        stats.batch(0, Duration::ZERO, None, true);
        let (bounds, counts) = stats.commit_histogram();
        assert_eq!(counts.iter().sum::<u64>(), 2);
        let bucket = bounds.iter().position(|b| *b >= 0.002).unwrap();
        assert_eq!(counts[bucket], 1);
        assert_eq!(counts[bounds.iter().position(|b| *b >= 0.021).unwrap()], 1);
    }
}
