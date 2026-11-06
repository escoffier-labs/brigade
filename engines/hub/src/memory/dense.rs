//! Immutable dense snapshots. The writer appends small deltas after commit.
//! A background compactor captures a watermark and the writer replays its tail.
use super::{embedder::normalize_vector, index::Snapshot, writer};
use rusqlite::Connection;
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    sync::{
        Arc, Mutex, RwLock,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

/// `memory_vector_log` rows the sweep keeps. A matrix whose watermark is
/// older than the oldest kept row reloads its embedder's vectors in full.
pub const LOG_KEEP: i64 = 4096;
/// How long a failed tail replay holds off the next compaction.
const REBUILD_RETRY: Duration = Duration::from_secs(60);

#[derive(Debug)]
pub struct Segment {
    pub hashes: Vec<String>,
    pub matrix: Vec<f32>,
}
#[derive(Clone, Default, Debug)]
pub struct Matrix {
    pub dimensions: usize,
    pub segments: Vec<Arc<Segment>>,
    pub rows: usize,
    pub indexed_seq: i64,
    /// The last `memory_vector_log` row this matrix reflects.
    pub log_seq: i64,
    /// None means all materialized rows. Deletions copy only this metadata.
    pub live: Option<Arc<HashSet<String>>>,
}
impl Matrix {
    fn has(&self, hash: &str) -> bool {
        self.live.as_ref().is_none_or(|live| live.contains(hash))
    }
    pub fn hashes(&self) -> HashSet<&str> {
        self.segments
            .iter()
            .flat_map(|s| s.hashes.iter().map(String::as_str))
            .filter(|h| self.has(h))
            .collect()
    }
    pub fn score(&self, snapshot: &Snapshot, allowed: &[bool], query: &[f32]) -> Vec<(u32, f64)> {
        if query.len() != self.dimensions {
            return Vec::new();
        }
        let mut scores = HashMap::new();
        for segment in &self.segments {
            for (hash, row) in segment
                .hashes
                .iter()
                .zip(segment.matrix.chunks_exact(self.dimensions))
            {
                if self.has(hash) {
                    scores.insert(
                        hash.as_str(),
                        row.iter()
                            .zip(query)
                            .map(|(a, b)| *a as f64 * *b as f64)
                            .sum::<f64>(),
                    );
                }
            }
        }
        snapshot
            .live(allowed)
            .into_iter()
            .filter_map(|n| scores.get(&*snapshot.doc(n).content_hash).map(|s| (n, *s)))
            .collect()
    }
    pub fn compact(&self) -> Self {
        // Latest append wins when a collected hash was embedded again. Rows
        // no longer present in the captured live table are omitted.
        let mut latest = HashMap::new();
        for old in &self.segments {
            for (hash, row) in old
                .hashes
                .iter()
                .zip(old.matrix.chunks_exact(self.dimensions))
            {
                if self.has(hash) {
                    latest.insert(hash.clone(), row);
                }
            }
        }
        let mut hashes: Vec<_> = latest.keys().cloned().collect();
        hashes.sort();
        let mut matrix = Vec::with_capacity(hashes.len() * self.dimensions);
        for hash in &hashes {
            matrix.extend_from_slice(latest[hash]);
        }
        let rows = hashes.len();
        Self {
            dimensions: self.dimensions,
            segments: vec![Arc::new(Segment { hashes, matrix })],
            rows,
            indexed_seq: self.indexed_seq,
            log_seq: self.log_seq,
            live: None,
        }
    }
    fn needs_rebuild(&self) -> bool {
        let main = self.segments.first().map_or(0, |s| s.hashes.len());
        self.segments.len() > 1 && (self.rows - main) * 10 > main
            || self
                .live
                .as_ref()
                .is_some_and(|live| (self.rows - live.len()) * 10 > self.rows)
    }
}
pub struct Rebuilt {
    id: String,
    matrix: Matrix,
    epoch: u64,
}
#[derive(Default)]
pub struct Index {
    publication: Mutex<()>,
    matrices: RwLock<HashMap<String, Arc<Matrix>>>,
    active: RwLock<Option<(String, usize)>>,
    sender: Mutex<Option<tokio::sync::mpsc::WeakSender<writer::Job>>>,
    epoch: AtomicU64,
    rebuilding: AtomicBool,
    /// Set when a tail replay failed, so the tick does not compact every second.
    rebuild_failed: Mutex<Option<Instant>>,
    stopping: AtomicBool,
    thread: Mutex<Option<std::thread::JoinHandle<()>>>,
    completed: tokio::sync::Notify,
    #[cfg(test)]
    gate: Mutex<Option<Arc<Gate>>>,
    /// Test only: the next refresh sleeps this long before it reads.
    #[cfg(test)]
    load_delay: Mutex<Option<Duration>>,
}
#[cfg(test)]
pub struct Gate {
    pub started: std::sync::Barrier,
    pub release: std::sync::Barrier,
}
impl Index {
    pub fn current(&self, id: &str) -> Option<Arc<Matrix>> {
        self.matrices.read().unwrap().get(id).cloned()
    }
    /// The active embedder id, which `put` and `retire` queue jobs for.
    pub fn active_id(&self) -> Option<String> {
        self.active
            .read()
            .unwrap()
            .as_ref()
            .map(|(id, _)| id.clone())
    }
    pub fn attach(&self, sender: tokio::sync::mpsc::WeakSender<writer::Job>) {
        *self.sender.lock().unwrap() = Some(sender);
    }
    pub fn configure(&self, active: Option<(String, usize)>) {
        let _publication = self.publication.lock().unwrap();
        self.epoch.fetch_add(1, Ordering::SeqCst);
        if active.is_none() {
            self.matrices.write().unwrap().clear();
        }
        *self.active.write().unwrap() = active;
    }
    pub fn stop(&self) {
        self.stopping.store(true, Ordering::SeqCst);
        if let Some(thread) = self.thread.lock().unwrap().take() {
            let _ = thread.join();
        }
    }
    pub fn drop_id(&self, id: &str) {
        let _publication = self.publication.lock().unwrap();
        self.epoch.fetch_add(1, Ordering::SeqCst);
        self.matrices.write().unwrap().remove(id);
    }
    fn heads(db: &Connection) -> rusqlite::Result<(i64, i64)> {
        db.query_row("SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='memory_vector_log'),0),COALESCE((SELECT max(seq) FROM memory_events),0)", [], |r| Ok((r.get(0)?, r.get(1)?)))
    }
    fn decode(bytes: Vec<u8>, dimensions: usize) -> rusqlite::Result<Vec<f32>> {
        if bytes.len() != dimensions * 4 {
            return Err(rusqlite::Error::InvalidQuery);
        }
        let floats = bytes
            .chunks_exact(4)
            .map(|b| f32::from_le_bytes(b.try_into().unwrap()))
            .collect();
        normalize_vector(floats, dimensions).map_err(|_| rusqlite::Error::InvalidQuery)
    }
    /// Every vector of one embedder, through `memory_vectors_embedder`.
    fn load(
        db: &Connection,
        id: &str,
        dimensions: usize,
        heads: (i64, i64),
    ) -> rusqlite::Result<Matrix> {
        let mut segment = Segment {
            hashes: Vec::new(),
            matrix: Vec::new(),
        };
        let mut stmt = db.prepare("SELECT content_hash,vector FROM memory_vectors WHERE embedder_id=?1 AND normalizer_version=1 ORDER BY content_hash")?;
        let mut rows = stmt.query([id])?;
        while let Some(row) = rows.next()? {
            segment.hashes.push(row.get(0)?);
            segment
                .matrix
                .extend(Self::decode(row.get(1)?, dimensions)?);
        }
        let rows = segment.hashes.len();
        Ok(Matrix {
            dimensions,
            segments: if rows == 0 {
                Vec::new()
            } else {
                vec![Arc::new(segment)]
            },
            rows,
            indexed_seq: heads.1,
            log_seq: heads.0,
            live: None,
        })
    }
    /// Replay the vector log after the base's watermark. A commit that did
    /// not touch vectors costs two primary-key reads and no vector scan.
    fn delta(db: &Connection, id: &str, base: &Matrix) -> rusqlite::Result<Matrix> {
        let heads = Self::heads(db)?;
        if heads.0 == base.log_seq {
            return Ok(Matrix {
                indexed_seq: heads.1,
                ..base.clone()
            });
        }
        let oldest: Option<i64> =
            db.query_row("SELECT min(seq) FROM memory_vector_log", [], |r| r.get(0))?;
        let fresh = base.log_seq == 0 && base.rows == 0;
        if fresh || heads.0 < base.log_seq || oldest.is_none_or(|o| o > base.log_seq + 1) {
            return Self::load(db, id, base.dimensions, heads);
        }
        // Only each hash's last operation in the tail decides its state.
        let mut last = BTreeMap::new();
        let mut log_seq = heads.0;
        {
            let mut stmt = db.prepare("SELECT seq,content_hash,present FROM memory_vector_log WHERE seq>?1 AND embedder_id=?2 ORDER BY seq")?;
            let mut rows = stmt.query(rusqlite::params![base.log_seq, id])?;
            while let Some(row) = rows.next()? {
                log_seq = log_seq.max(row.get(0)?);
                last.insert(row.get::<_, String>(1)?, row.get::<_, i64>(2)? != 0);
            }
        }
        let mut segment = Segment {
            hashes: Vec::new(),
            matrix: Vec::new(),
        };
        let mut removed = Vec::new();
        let mut stmt = db.prepare("SELECT vector FROM memory_vectors WHERE embedder_id=?1 AND normalizer_version=1 AND content_hash=?2")?;
        for (hash, present) in last {
            let vector: Option<Vec<u8>> = if present {
                stmt.query_row(rusqlite::params![id, &hash], |r| r.get(0))
                    .map(Some)
                    .or_else(|e| match e {
                        rusqlite::Error::QueryReturnedNoRows => Ok(None),
                        e => Err(e),
                    })?
            } else {
                None
            };
            match vector {
                Some(bytes) => {
                    segment.matrix.extend(Self::decode(bytes, base.dimensions)?);
                    segment.hashes.push(hash);
                }
                None => removed.push(hash),
            }
        }
        let mut next = Matrix {
            indexed_seq: heads.1,
            log_seq,
            ..base.clone()
        };
        let known = (!removed.is_empty() || base.live.is_some()).then(|| base.hashes());
        let removes = known
            .as_ref()
            .is_some_and(|k| removed.iter().any(|h| k.contains(h.as_str())));
        if removes || base.live.is_some() {
            // O(N) clone per commit after a deletion; `needs_rebuild` bounds how long it lasts.
            let mut live: HashSet<String> = match &base.live {
                Some(live) => (**live).clone(),
                None => known
                    .unwrap_or_default()
                    .into_iter()
                    .map(String::from)
                    .collect(),
            };
            for hash in &removed {
                live.remove(hash);
            }
            live.extend(segment.hashes.iter().cloned());
            next.live = Some(Arc::new(live));
        }
        next.rows += segment.hashes.len();
        if !segment.hashes.is_empty() {
            next.segments.push(Arc::new(segment));
        }
        Ok(next)
    }
    pub fn refresh(
        self: &Arc<Self>,
        db: &Connection,
        id: &str,
        dimensions: usize,
    ) -> rusqlite::Result<Arc<Matrix>> {
        #[cfg(test)]
        if let Some(delay) = self.load_delay.lock().unwrap().take() {
            std::thread::sleep(delay);
        }
        let _publication = self.publication.lock().unwrap();
        let current = self.current(id);
        // The first load installs a matrix even when it holds no row, so
        // "no matrix" always means "no load has succeeded yet".
        let first = current.is_none();
        let old = current.unwrap_or_else(|| {
            Arc::new(Matrix {
                dimensions,
                ..Matrix::default()
            })
        });
        let next = Self::delta(db, id, &old)?;
        if !first && next.log_seq == old.log_seq && next.indexed_seq == old.indexed_seq {
            if old.needs_rebuild() {
                self.rebuild(id, old.clone());
            }
            return Ok(old);
        }
        let next = Arc::new(next);
        self.matrices
            .write()
            .unwrap()
            .insert(id.into(), next.clone());
        if next.needs_rebuild() {
            self.rebuild(id, next.clone());
        }
        Ok(next)
    }
    /// Called by the memory writer only after releasing both write locks.
    pub fn after_commit(self: &Arc<Self>, db: &Connection) {
        let active = self.active.read().unwrap().clone();
        if let Some((id, dimensions)) = active {
            let _ = self.refresh(db, &id, dimensions);
        }
    }
    fn rebuild(self: &Arc<Self>, id: &str, captured: Arc<Matrix>) {
        if self.stopping.load(Ordering::SeqCst)
            || self.rebuilding.load(Ordering::SeqCst)
            || self
                .rebuild_failed
                .lock()
                .unwrap()
                .is_some_and(|at| at.elapsed() < REBUILD_RETRY)
        {
            return;
        }
        let sender = self.sender.lock().unwrap().clone();
        let Some(sender) = sender else {
            return;
        };
        if self.rebuilding.swap(true, Ordering::SeqCst) {
            return;
        }
        if let Some(thread) = self.thread.lock().unwrap().take() {
            let _ = thread.join();
        }
        let weak = Arc::downgrade(self);
        let id = id.to_string();
        let epoch = self.epoch.load(Ordering::SeqCst);
        #[cfg(test)]
        let gate = self.gate.lock().unwrap().take();
        let thread = std::thread::Builder::new()
            .name("memory-dense-rebuild".into())
            .spawn(move || {
                #[cfg(test)]
                if let Some(gate) = gate {
                    gate.started.wait();
                    gate.release.wait();
                }
                let rebuilt = Rebuilt {
                    id,
                    matrix: captured.compact(),
                    epoch,
                };
                if let Some(index) = weak.upgrade() {
                    let sent = !index.stopping.load(Ordering::SeqCst)
                        && sender.upgrade().is_some_and(|sender| {
                            sender
                                .try_send(writer::Job::DenseRebuilt(Box::new(rebuilt)))
                                .is_ok()
                        });
                    if !sent {
                        index.rebuilding.store(false, Ordering::SeqCst);
                        index.completed.notify_waiters();
                    }
                }
            });
        match thread {
            Ok(thread) => *self.thread.lock().unwrap() = Some(thread),
            Err(_) => {
                self.rebuilding.store(false, Ordering::SeqCst);
                self.completed.notify_waiters();
            }
        }
    }
    /// The writer replays the committed tail and atomically publishes. A
    /// rebuild captured before a mode switch or drop cannot resurrect rows.
    pub fn installed(&self, rebuilt: Rebuilt, db: &Connection) {
        let _publication = self.publication.lock().unwrap();
        if !self.stopping.load(Ordering::SeqCst)
            && self.epoch.load(Ordering::SeqCst) == rebuilt.epoch
        {
            match Self::delta(db, &rebuilt.id, &rebuilt.matrix) {
                Ok(next) => {
                    *self.rebuild_failed.lock().unwrap() = None;
                    self.matrices
                        .write()
                        .unwrap()
                        .insert(rebuilt.id, Arc::new(next));
                }
                Err(_) => *self.rebuild_failed.lock().unwrap() = Some(Instant::now()),
            }
        }
        self.rebuilding.store(false, Ordering::SeqCst);
        self.completed.notify_waiters();
    }
    #[cfg(test)]
    pub fn delay_next_load(&self, delay: Duration) {
        *self.load_delay.lock().unwrap() = Some(delay);
    }
    #[cfg(test)]
    pub fn rebuild_held(&self) -> bool {
        self.rebuild_failed.lock().unwrap().is_some()
    }
    #[cfg(test)]
    pub fn pause_next(&self) -> Arc<Gate> {
        let gate = Arc::new(Gate {
            started: std::sync::Barrier::new(2),
            release: std::sync::Barrier::new(2),
        });
        *self.gate.lock().unwrap() = Some(gate.clone());
        gate
    }
    #[cfg(test)]
    pub async fn wait_rebuild(&self) {
        loop {
            let wait = self.completed.notified();
            tokio::pin!(wait);
            wait.as_mut().enable();
            if !self.rebuilding.load(Ordering::SeqCst) {
                return;
            }
            wait.await;
        }
    }
}
